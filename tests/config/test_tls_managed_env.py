"""The HTTPS environment a container install carries in its own volume.

A container's environment is fixed at creation, so this file is the only place
an HTTPS switch can put settings that the *next boot* will read. Everything
here is about the two ways that could go wrong: not being read when it should
be, and being read when it should not.
"""
import pytest

from app.config import tls_managed_env as managed


@pytest.fixture
def volume(monkeypatch, tmp_path):
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setenv("INSTALL_MODE", "docker")
    for key in managed.MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Neutralise the container signals explicitly: CI itself may run inside a
    # container, and the desktop image bakes VNC_PASSWORD. Tests about the
    # fallback point the markers at files they create.
    monkeypatch.delenv("VNC_PASSWORD", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", tmp_path / "no-dockerenv")
    monkeypatch.setattr(managed, "_POD_MARKER", tmp_path / "no-serviceaccount")
    (tmp_path / "tls").mkdir(parents=True, exist_ok=True)
    return tmp_path


def in_a_container(volume, monkeypatch):
    """Put the process inside a Docker container, as far as the marker goes."""
    marker = volume / "dockerenv"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(managed, "_CONTAINER_MARKER", marker)


def write(volume, text: str, *, encoding: str = "utf-8") -> None:
    (volume / "tls" / "managed-env").write_text(text, encoding=encoding)


def test_the_key_allowlist_matches_the_one_the_switch_writes():
    """The duplication is deliberate — settings.py imports this module during
    its own import, so it cannot reach tls_transition. This is what stops the
    two drifting into a switch that writes keys the boot never applies."""
    from app.config.tls_transition import _NATIVE_ENV_KEYS

    assert set(managed.MANAGED_KEYS) == set(_NATIVE_ENV_KEYS)


def test_a_missing_or_unreadable_file_is_simply_nothing(volume):
    assert managed.read() == {}
    assert managed.load_into_environ() == {}


def test_values_are_applied_over_the_container_environment(volume, monkeypatch):
    """The whole point: the values being replaced are the ones baked into the
    container at creation, which is exactly what the switch is changing."""
    monkeypatch.setenv("CREMIND_SSL", "")
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    write(volume, "CREMIND_SSL=true\nAPP_URL=https://localhost:1515\n")

    applied = managed.load_into_environ()

    assert applied == {"CREMIND_SSL": "true", "APP_URL": "https://localhost:1515"}
    import os
    assert os.environ["CREMIND_SSL"] == "true"
    assert os.environ["APP_URL"] == "https://localhost:1515"


def test_only_the_keys_the_switch_owns_are_honoured(volume):
    write(volume, "APP_URL=https://a\nPATH=/evil\nDATABASE_URL=postgres://x\n")

    applied = managed.load_into_environ()

    assert applied == {"APP_URL": "https://a"}
    import os
    assert os.environ.get("PATH") != "/evil"
    assert "DATABASE_URL" not in os.environ


def test_comments_blank_lines_quotes_and_a_bom_are_all_tolerated(volume):
    write(volume, '﻿# written by Cremind\n\nAPP_URL="https://q"\n  CREMIND_SSL = true \n')

    assert managed.read() == {"APP_URL": "https://q", "CREMIND_SSL": "true"}


def test_the_file_retires_itself_once_the_deployment_agrees(volume, monkeypatch):
    """Once the container was recreated with the values baked in, the file has
    nothing left to say — and keeping it would silently outrank whatever the
    operator edits next."""
    monkeypatch.setenv("CREMIND_SSL", "auto")
    monkeypatch.setenv("APP_URL", "https://localhost:1515")
    write(volume, "CREMIND_SSL=auto\nAPP_URL=https://localhost:1515\n")

    assert managed.load_into_environ() == {}
    assert not (volume / "tls" / "managed-env").exists()


def test_a_partial_agreement_still_applies(volume, monkeypatch):
    monkeypatch.setenv("CREMIND_SSL", "auto")
    monkeypatch.setenv("APP_URL", "http://localhost:1515")  # stale
    write(volume, "CREMIND_SSL=auto\nAPP_URL=https://localhost:1515\n")

    assert managed.load_into_environ()["APP_URL"] == "https://localhost:1515"
    assert (volume / "tls" / "managed-env").exists()


@pytest.mark.parametrize("mode", ["native", "kubernetes", "", "electron"])
def test_no_other_deployment_reads_it(volume, monkeypatch, mode):
    """Kubernetes is excluded even though its PVC would hold the file: enabling
    HTTPS there moves the Service, the probes and the proxy sidecar together,
    so honouring this would let the pod believe in a switch it never made.

    Every case but ``kubernetes`` holds only *outside* a container — the fixture
    neutralises the marker. ``test_a_container_that_never_said_is_still_docker``
    and ``test_the_overlay_is_read_by_a_container_that_claims_native`` are the
    other half."""
    monkeypatch.setenv("INSTALL_MODE", mode)
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    write(volume, "APP_URL=https://localhost:1515\n")

    assert managed.load_into_environ() == {}
    import os
    assert os.environ["APP_URL"] == "http://localhost:1515"
    assert (volume / "tls" / "managed-env").exists()  # not retired either


# ── which container this is, when INSTALL_MODE does not say ───────────────


def test_a_container_mode_is_authoritative_when_it_names_one(volume, monkeypatch):
    """``docker`` and ``kubernetes`` are taken at their word even against a
    contradicting signal: a deployment author wrote them, and they are the only
    way a containerd install — which has no ``/.dockerenv`` — can say what it
    is. The host modes are the ones a container's environment cannot really be
    in; see ``test_a_host_mode_inside_a_container_is_the_container``."""
    in_a_container(volume, monkeypatch)
    for mode in ("docker", "kubernetes"):
        monkeypatch.setenv("INSTALL_MODE", mode)
        assert managed.effective_install_mode() == mode
        assert managed.install_mode_provenance() == managed.INSTALL_MODE_SAID
        assert managed.is_container_install() is (mode == "docker")
    # Operators type these by hand; the rest of the HTTPS path always
    # lower-cased the variable, so this keeps doing so.
    monkeypatch.setenv("INSTALL_MODE", "  Docker ")
    assert managed.effective_install_mode() == "docker"


@pytest.mark.parametrize("raw", ["", "podman", "compose", "__INSTALL_MODE__"])
def test_a_container_that_never_said_is_still_docker(volume, monkeypatch, raw):
    """``docker run`` on the image, a hand-written compose file, a ``.env`` that
    predates the key: the variable is absent or meaningless, and the container
    marker is the only thing left that knows. Every other reader of the mode
    already fell back to it; the HTTPS path reading the variable raw is what
    showed a Compose operator the Ctrl+C runbook for a native install and
    persisted their switch into a ``.env`` the container environment shadows."""
    monkeypatch.setenv("INSTALL_MODE", raw)
    in_a_container(volume, monkeypatch)

    assert managed.effective_install_mode() == "docker"
    assert managed.is_container_install() is True
    assert managed.install_mode_provenance() == managed.INSTALL_MODE_INFERRED

    # And so the overlay is honoured, which is the whole point.
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    write(volume, "APP_URL=https://localhost:1515\n")
    assert managed.load_into_environ() == {"APP_URL": "https://localhost:1515"}


def test_the_desktop_image_s_vnc_password_is_the_same_signal(volume, monkeypatch):
    """The desktop image bakes ``VNC_PASSWORD``; a pre-flavor ``.env`` carries
    nothing else. Same fallback the Developer page and the shutdown path use."""
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    monkeypatch.setenv("VNC_PASSWORD", "changeme")

    assert managed.effective_install_mode() == "docker"
    assert managed.is_container_install() is True


def test_a_host_mode_inside_a_container_is_the_container(volume, monkeypatch):
    """The reported install, three times over: a Compose container whose
    environment says ``INSTALL_MODE=native``.

    This used to be honoured, on the reasoning that a developer running the
    checkout inside a devcontainer said what they meant. But a container's
    environment is fixed when the container is created, and such a value is
    almost always a leak rather than a statement: Compose resolves ``${VAR}``
    from the shell that runs ``docker compose up`` before the project's
    ``.env``, and the native install's PowerShell shim used to leave
    ``INSTALL_MODE=native`` in the session it ran in. Believing it told a Docker
    user to press Ctrl+C in a terminal that does not exist, and wrote the switch
    into a ``.env`` the container environment shadows.

    The devcontainer developer is not abandoned: their switch now goes to the
    overlay and applies on their next ``cremind serve``. ``kubernetes`` is still
    taken at its word."""
    in_a_container(volume, monkeypatch)
    for mode in ("native", "custom"):
        monkeypatch.setenv("INSTALL_MODE", mode)
        assert managed.effective_install_mode() == "docker"
        assert managed.is_container_install() is True
        assert managed.install_mode_provenance() == managed.INSTALL_MODE_OVERRIDDEN

    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    assert managed.effective_install_mode() == "kubernetes"
    assert managed.is_container_install() is False
    assert managed.install_mode_provenance() == managed.INSTALL_MODE_SAID


def test_the_overlay_is_read_by_a_container_that_claims_native(volume, monkeypatch):
    """The reported install's next boot. Until now ``load_into_environ`` was
    gated behind the same mistaken reading, so the switch saved in the volume
    was not even looked at and the container came back on HTTP."""
    monkeypatch.setenv("INSTALL_MODE", "native")
    in_a_container(volume, monkeypatch)
    monkeypatch.setenv("APP_URL", "http://localhost:1515")
    write(volume, "APP_URL=https://localhost:1515\nCREMIND_SSL=true\n")

    applied = managed.load_into_environ()

    assert applied == {"APP_URL": "https://localhost:1515", "CREMIND_SSL": "true"}
    import os
    assert os.environ["APP_URL"] == "https://localhost:1515"


@pytest.mark.parametrize("signal", ["serviceaccount", "KUBERNETES_SERVICE_HOST"])
@pytest.mark.parametrize("claim", [None, "native", "custom"])
def test_a_pod_is_kubernetes_never_docker(volume, monkeypatch, signal, claim):
    """A cluster whose runtime still writes ``/.dockerenv`` (cri-dockerd), or
    the desktop image's baked VNC_PASSWORD, in a hand-rolled manifest without
    the chart's ``INSTALL_MODE``: inferring Docker there would hand the pod a
    self-restarting switch its Service and probes never made. Either pod
    signal is enough — the service-account mount can be opted out of
    (``automountServiceAccountToken: false``), the kubelet's variable cannot.

    It answers ``kubernetes`` rather than "nothing known" so that every reader
    is right about a pod: the kubelet supervises it, its HTTPS runbook is the
    Helm one, and CA trust is a container's. ``is_container_install`` stays
    False, so the overlay is still never read there."""
    if claim is None:
        monkeypatch.delenv("INSTALL_MODE", raising=False)
    else:
        monkeypatch.setenv("INSTALL_MODE", claim)
    in_a_container(volume, monkeypatch)
    if signal == "serviceaccount":
        pod = volume / "serviceaccount"
        pod.mkdir()
        monkeypatch.setattr(managed, "_POD_MARKER", pod)
    else:
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")

    assert managed.effective_install_mode() == "kubernetes"
    assert managed.is_container_install() is False
    assert managed.install_mode_provenance() == (
        managed.INSTALL_MODE_OVERRIDDEN if claim else managed.INSTALL_MODE_INFERRED
    )


def test_the_known_modes_are_the_install_catalog_s():
    """A third hand-copied list of the catalog's modes (with ``server.py``'s and
    the catalog itself). A mode added to the catalog but not here would be
    treated as absent — and inferred to be Docker inside a container."""
    from app.config.install_catalog import load_install_catalog

    catalog = load_install_catalog()
    expected = set(catalog.get("modes") or {}) | set(catalog.get("mode_rules") or {})

    assert set(managed.KNOWN_INSTALL_MODES) == expected


def test_no_signal_at_all_is_not_a_container(volume, monkeypatch):
    monkeypatch.delenv("INSTALL_MODE", raising=False)

    assert managed.effective_install_mode() == ""
    assert managed.is_container_install() is False
    assert managed.install_mode_provenance() == managed.INSTALL_MODE_SAID


def test_a_host_mode_outside_a_container_is_honoured(volume, monkeypatch):
    """Nothing about a real native install changes: no marker, no VNC password,
    no pod signal, so the claim stands and nothing is worth logging."""
    for mode in ("native", "custom"):
        monkeypatch.setenv("INSTALL_MODE", mode)
        assert managed.effective_install_mode() == mode
        assert managed.is_container_install() is False
        assert managed.install_mode_provenance() == managed.INSTALL_MODE_SAID


def test_the_file_is_never_executed_as_shell(volume):
    """It is parsed, not sourced. A value is a value whatever it looks like."""
    write(volume, "APP_URL=https://$(whoami).example\n")

    assert managed.read()["APP_URL"] == "https://$(whoami).example"
