"""Unit tests for the shared runtime-environment description.

Every fact here comes from an environment variable an installer wrote, so the
tests are a matrix over those variables: each row is an install someone really
has (a dev checkout, the desktop image, the basic image, a pre-flavor image, a
Helm pod, the Electron app) and what the description must say about it.

Three traps have their own rows because each was a live bug: ``BaseConfig.ENV``
defaults to ``production`` when unset, which would make every native dev box
call itself a server deployment; ``ENV`` is *pinned* to ``production`` by the
compose file inside every container, so reading it there told every local
Docker user they were on a server; and a Docker image predating the flavor
split sets no ``CREMIND_IMAGE_FLAVOR`` yet *is* a desktop image, so "no flavor"
must not read as "no VNC" on a container.

The Kubernetes rows are a matrix of a different kind: a pod started by an older
chart states none of its own names, and the whole point of the identity block is
that it degrades to placeholders instead of printing a command that fails. So
every fallback has a row, and so does the case where nothing at all is known.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import runtime_env


# Everything the description reads. Cleared before each case so the developer
# machine running the suite can't leak its own install into the matrix.
_ENV_KEYS = (
    "INSTALL_MODE",
    "CREMIND_IMAGE_FLAVOR",
    "CREMIND_UPGRADE_CHANNEL",
    "ENV",
    "SETUP_WIZARD_ENV",
    "CREMIND_ELECTRON_PARENT",
    "CREMIND_SUPERVISED",
    "VNC_PASSWORD",
    "CREMIND_TIMEZONE",
    # Kubernetes identity: what the chart states about the pod, plus the two
    # signals the fallbacks read when it states nothing.
    "CREMIND_K8S_NAMESPACE",
    "CREMIND_K8S_RELEASE",
    "CREMIND_K8S_WORKLOAD",
    "CREMIND_K8S_SERVICE_PORT",
    "KUBERNETES_SERVICE_HOST",
    "HOSTNAME",
    # The VNC descriptor. HOSTNAME and CREMIND_SSL in particular are set on
    # plenty of developer machines, so leaving them in place would make these
    # rows pass or fail depending on whose shell ran them.
    "CREMIND_NOVNC_URL",
    "NOVNC_PORT",
    "CREMIND_SSL",
    "CREMIND_TLS_TERMINATION",
    "CREMIND_COMPOSE_ENV_FILE",
    "APP_URL",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One clean, uncached environment per case.

    The container marker is pointed at a path that cannot exist because CI
    itself may run inside a container — otherwise every "native" row here would
    flip to Docker on the build machine and nowhere else. The service-account
    namespace file is pinned the same way and for the same reason: a suite run
    inside a real cluster would otherwise inherit that cluster's namespace, and
    every "the pod cannot name itself" row would pass everywhere but there.
    ``/proc/cpuinfo`` is the third of them: the CPU flags a build machine
    advertises are its own, so leaving that path alone would let the runner's
    hardware decide whether ``x86_64_level`` is ``v2`` or ``v3`` — and every row
    below would pass or fail depending on whose CPU ran it.

    ``CREMIND_INSTALL_DIR`` moves to an empty tmp dir so ``_novnc_port`` cannot
    read the developer's own ``docker/.env``.
    """
    from app.config.settings import BaseConfig

    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime_env, "_CONTAINER_MARKER", Path("/nonexistent/.dockerenv"))
    monkeypatch.setattr(runtime_env, "_SA_NAMESPACE_FILE", Path("/nonexistent/namespace"))
    monkeypatch.setattr(runtime_env, "_CPUINFO_PATH", Path("/nonexistent/cpuinfo"))
    monkeypatch.setattr(BaseConfig, "CREMIND_INSTALL_DIR", str(tmp_path / "install"))
    runtime_env.describe_runtime_environment.cache_clear()
    yield
    runtime_env.describe_runtime_environment.cache_clear()


def _describe(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runtime_env.describe_runtime_environment.cache_clear()
    return runtime_env.describe_runtime_environment()


# ── the install matrix ────────────────────────────────────────────────────

_CASES = [
    pytest.param(
        {"ENV": "local", "CREMIND_UPGRADE_CHANNEL": "dev"},
        {
            "install_mode": "native",
            "deployment": "local",
            "release_channel": "dev",
            "container": False,
            "image_flavor": None,
            "vnc_enabled": False,
            "supervised": False,
            "electron": False,
        },
        id="native-local-dev",
    ),
    pytest.param(
        {"INSTALL_MODE": "docker", "CREMIND_IMAGE_FLAVOR": "desktop", "ENV": "local"},
        {
            "install_mode": "docker",
            "deployment": "local",
            "release_channel": "production",
            "container": True,
            "image_flavor": "desktop",
            "vnc_enabled": True,
            "supervised": True,
        },
        id="docker-desktop-production",
    ),
    pytest.param(
        {"INSTALL_MODE": "docker", "CREMIND_IMAGE_FLAVOR": "basic", "ENV": "production"},
        {
            "install_mode": "docker",
            "deployment": "server",
            "container": True,
            "image_flavor": "basic",
            "vnc_enabled": False,
            "supervised": True,
        },
        id="docker-basic-headless",
    ),
    pytest.param(
        # Every real container looks like this: docker-compose.yml pins
        # ``ENV: production`` in the service's ``environment:`` block, so the
        # operator's actual pick only survives in SETUP_WIZARD_ENV. Reading ENV
        # first told every Docker user they were on a server deployment.
        {"INSTALL_MODE": "docker", "ENV": "production", "SETUP_WIZARD_ENV": "local"},
        {"install_mode": "docker", "deployment": "local"},
        id="docker-wizard-preset-beats-the-pinned-ENV",
    ),
    pytest.param(
        # A preset the table doesn't know came from ``--wizard-preset``, which
        # the installer offers on a custom deployment and never validates.
        {"INSTALL_MODE": "docker", "ENV": "production", "SETUP_WIZARD_ENV": "my-lab"},
        {"install_mode": "docker", "deployment": "custom"},
        id="docker-unknown-wizard-preset-is-custom",
    ),
    pytest.param(
        # A pre-flavor image: no CREMIND_IMAGE_FLAVOR, but it has the desktop.
        {"INSTALL_MODE": "docker", "ENV": "local"},
        {
            "install_mode": "docker",
            "image_flavor": None,
            "vnc_enabled": True,
            "container": True,
        },
        id="docker-pre-flavor-image",
    ),
    pytest.param(
        # The Helm chart writes INSTALL_MODE=kubernetes; ENV says nothing
        # useful there, and the deployment is the cluster either way.
        {"INSTALL_MODE": "kubernetes", "ENV": "production", "CREMIND_UPGRADE_CHANNEL": "test"},
        {
            "install_mode": "kubernetes",
            "deployment": "kubernetes",
            "release_channel": "test",
            "container": True,
            "vnc_enabled": True,
            "supervised": True,
        },
        id="kubernetes",
    ),
    pytest.param(
        {"ENV": "custom", "SETUP_WIZARD_ENV": "server"},
        {"install_mode": "native", "deployment": "custom"},
        id="native-custom-deployment",
    ),
    pytest.param(
        # No ENV line at all (the Docker image leaves it to the container
        # default) — the wizard preset is the fallback, and `docker` is a
        # preset, not a deployment.
        {"SETUP_WIZARD_ENV": "docker"},
        {"deployment": "local"},
        id="wizard-preset-docker-is-a-local-deployment",
    ),
    pytest.param(
        {"SETUP_WIZARD_ENV": "server"},
        {"deployment": "server"},
        id="wizard-preset-server",
    ),
    pytest.param(
        {"CREMIND_ELECTRON_PARENT": "1", "ENV": "local"},
        {"install_mode": "native", "electron": True, "supervised": True},
        id="electron-desktop-app",
    ),
    pytest.param(
        {"CREMIND_SUPERVISED": "1", "ENV": "local"},
        {"install_mode": "native", "supervised": True, "electron": False},
        id="native-with-a-boot-service",
    ),
    pytest.param(
        # An older Docker .env predating INSTALL_MODE: the desktop image's
        # VNC_PASSWORD is the only thing left saying we're in a container.
        # ``supervised`` used to expect False here, because it tested the raw
        # INSTALL_MODE while every other field went through
        # ``detect_install_mode``. That made it the one field that could
        # contradict the ``install_mode`` next to it — the agent's prompt line
        # called this machine a "Docker install ... no supervisor". It is the
        # same Docker install whose restart policy brings it back, so the
        # resolved mode answers here too.
        {"VNC_PASSWORD": "secret", "ENV": "local"},
        {"install_mode": "docker", "container": True, "supervised": True},
        id="legacy-docker-env-without-install-mode",
    ),
]


@pytest.mark.parametrize("env,expected", _CASES)
def test_describes_the_install(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: dict,
) -> None:
    described = _describe(monkeypatch, env)
    for key, value in expected.items():
        assert described[key] == value, key


def test_unset_env_is_not_a_server_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BaseConfig.ENV`` defaults to ``production``; the description must not.

    Reading it through BaseConfig would report every ``cremind serve`` in a
    checkout as a server deployment, which is the opposite of the truth.
    """
    assert _describe(monkeypatch, {})["deployment"] == "local"


def test_supervised_follows_the_resolved_install_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """One resolved fact, not two — ``supervised`` asks the same question.

    An install whose ``.env`` predates INSTALL_MODE is still Docker: compose
    sets ``restart: unless-stopped`` and brings it back. Testing the raw
    variable here answered "no supervisor" for the very install the fallback
    exists to describe, and the restart consumers had already decided
    otherwise — both the Developer page's dialog and ``cremind server restart``
    pick their copy from ``install_mode`` first and only fall through to this
    field on a native install.
    """
    described = _describe(monkeypatch, {"VNC_PASSWORD": "secret", "ENV": "local"})

    assert described["install_mode"] == "docker"
    assert described["supervised"] is True


def test_carries_the_process_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-derived half: versions, paths and the boot timezone."""
    from app.__version__ import __version__
    from app.config.settings import BaseConfig

    described = _describe(monkeypatch, {"CREMIND_TIMEZONE": "Asia/Ho_Chi_Minh"})

    assert described["backend_version"] == __version__
    assert described["python_version"].count(".") == 2
    assert described["os"] and described["os_release"] is not None
    assert described["app_url"] == BaseConfig.APP_URL
    assert described["host"] == BaseConfig.HOST
    assert described["system_dir"] == BaseConfig.CREMIND_SYSTEM_DIR
    assert described["install_dir"] == BaseConfig.CREMIND_INSTALL_DIR
    assert described["boot_timezone"] == "Asia/Ho_Chi_Minh"


def test_boot_timezone_is_the_env_var_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is named for what it holds: CREMIND_TIMEZONE, nothing more.

    The zone anything actually schedules in is per-profile and comes from
    :mod:`app.config.timezone`; this field is only the boot default it may fall
    through to, so a blank value here is not "no timezone configured".
    """
    described = _describe(monkeypatch, {})
    assert described["boot_timezone"] == ""
    assert "timezone" not in described


# ── caching ───────────────────────────────────────────────────────────────


def test_description_is_cached_and_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    """It feeds a prompt-cached system prompt, so it must not move mid-process.

    And each caller gets its own copy: ``/api/system/environment`` adds a key
    to what it gets back, which would otherwise leak into the agent's line.

    The copy has to be deep now that ``vnc`` and ``kubernetes`` are nested
    blocks. A shallow copy hands every caller the same inner dict and the same
    command list, so the tray endpoint trimming the descriptor to its public
    subset — or anything stamping a rendered URL into it — would rewrite what
    the next request sees, for the life of the process.
    """
    first = _describe(monkeypatch, {"INSTALL_MODE": "docker"})
    first["install_mode"] = "mutated"
    first["vnc"]["access"] = "mutated"
    first["vnc"]["port_forward_commands"].append("mutated")

    monkeypatch.setenv("INSTALL_MODE", "native")
    second = runtime_env.describe_runtime_environment()

    assert second["install_mode"] == "docker"
    assert second["vnc"]["access"] == "direct"
    assert second["vnc"]["port_forward_commands"] == []

    runtime_env.describe_runtime_environment.cache_clear()
    assert runtime_env.describe_runtime_environment()["install_mode"] == "native"


def test_mutable_fields_are_read_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTPS switch rewrites ``BaseConfig.APP_URL`` in this very process.

    ``app.config.tls_transition`` reassigns it when a switch is armed and the
    follow-up restart is optional, so a cached copy makes one
    ``/api/system/environment`` body contradict itself — its
    ``deployment_custom_fields.public_url`` reads the attribute live. The same
    goes for the System Directory, which the Setup Wizard's submit handler can
    relocate on first setup. The prompt line must not move with them.
    """
    from app.config.settings import BaseConfig

    _describe(monkeypatch, {"INSTALL_MODE": "docker"})
    before = runtime_env.runtime_environment_prompt_line()

    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example:1515")
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", "/relocated/.cremind")

    described = runtime_env.describe_runtime_environment()
    assert described["app_url"] == "https://cremind.example:1515"
    assert described["system_dir"] == "/relocated/.cremind"
    assert runtime_env.runtime_environment_prompt_line() == before


def test_image_flavor_reads_the_env_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """``app.api.features`` re-exports this one and its tests set the var and
    call straight through, so it must never pick up the description's cache."""
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "  DESKTOP  ")
    assert runtime_env.get_image_flavor() == "desktop"
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "garbage")
    assert runtime_env.get_image_flavor() is None


# ── the agent's prompt line ───────────────────────────────────────────────


def test_prompt_line_for_a_docker_desktop_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe(monkeypatch, {
        "INSTALL_MODE": "docker",
        "CREMIND_IMAGE_FLAVOR": "desktop",
        "ENV": "local",
    })
    line = runtime_env.runtime_environment_prompt_line()

    assert "\n" not in line
    assert line.startswith("Runtime environment: Docker install")
    assert "desktop image" in line
    assert "VNC desktop enabled" in line
    assert "local deployment" in line
    assert "production release channel" in line
    assert "restarts supervised" in line


def test_prompt_line_for_a_native_dev_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe(monkeypatch, {"ENV": "local", "CREMIND_UPGRADE_CHANNEL": "dev"})
    line = runtime_env.runtime_environment_prompt_line()

    assert "\n" not in line
    assert line == (
        "Runtime environment: native install, local deployment, "
        "dev release channel, no supervisor."
    )
    # A native install has no VNC desktop at all — don't mention one.
    assert "VNC" not in line


def test_prompt_line_says_when_the_container_has_no_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The basic image is the one case where "is VNC on?" answers no on Docker."""
    _describe(monkeypatch, {"INSTALL_MODE": "docker", "CREMIND_IMAGE_FLAVOR": "basic"})
    line = runtime_env.runtime_environment_prompt_line()

    assert "basic image" in line
    assert "no VNC desktop" in line


def test_prompt_line_for_kubernetes(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe(monkeypatch, {"INSTALL_MODE": "kubernetes", "CREMIND_UPGRADE_CHANNEL": "test"})
    line = runtime_env.runtime_environment_prompt_line()

    assert line.startswith("Runtime environment: Kubernetes install")
    assert "kubernetes deployment" in line
    assert "test release channel" in line


def test_prompt_line_names_the_electron_app(monkeypatch: pytest.MonkeyPatch) -> None:
    _describe(monkeypatch, {"CREMIND_ELECTRON_PARENT": "1"})
    assert "Electron desktop app" in runtime_env.runtime_environment_prompt_line()


@pytest.mark.parametrize("env,_expected", _CASES)
def test_prompt_line_never_contradicts_itself_about_the_supervisor(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], _expected: dict,
) -> None:
    """No install may render as a container that nothing restarts.

    The whole line is one sentence in a prompt-cached system message, so a
    field derived from a second, weaker signal shows up as the agent being told
    "Docker install (VNC desktop enabled) ... no supervisor" about one machine.
    Run over the whole matrix rather than the one row that broke it, because
    the invariant is what matters: container-ness and supervision come from the
    same resolved install mode.
    """
    _describe(monkeypatch, env)
    line = runtime_env.runtime_environment_prompt_line()

    container_label = "Docker install" in line or "Kubernetes install" in line
    assert not (container_label and "no supervisor" in line), line


def test_prompt_line_is_stable_within_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte-identical between turns, or the prompt cache misses on every turn."""
    _describe(monkeypatch, {"INSTALL_MODE": "docker"})
    assert (
        runtime_env.runtime_environment_prompt_line()
        == runtime_env.runtime_environment_prompt_line()
    )


# ── install-mode detection (shared with the Codex sign-in flow) ────────────


def test_detect_install_mode_honours_a_caller_supplied_marker(tmp_path: Path) -> None:
    """``app.api.llm_codex_flow`` keeps its own patchable marker and passes it
    in; a marker that exists means Docker even with no INSTALL_MODE."""
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")

    assert runtime_env.detect_install_mode(container_marker=marker) == "docker"
    assert runtime_env.detect_install_mode(container_marker=tmp_path / "absent") == "native"


def test_unknown_install_mode_falls_back_to_native(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_active_install_mode`` rejects a value the catalog doesn't define,
    and nothing else says container — so this is a native install."""
    monkeypatch.setenv("INSTALL_MODE", "not-a-mode")
    assert runtime_env.detect_install_mode() == "native"


def test_is_container_answers_for_both_container_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One question, two signals that miss in opposite directions.

    ``/.dockerenv`` is never written on a Kubernetes pod, and ``INSTALL_MODE``
    is missing from a Docker ``.env`` that predates the key, so anything asking
    "is ``~`` a throwaway image layer here?" (the coding-CLI login homes) has to
    accept either one.
    """
    assert runtime_env.is_container("native") is False
    assert runtime_env.is_container("docker") is True
    assert runtime_env.is_container("kubernetes") is True

    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime_env, "_CONTAINER_MARKER", marker)
    assert runtime_env.is_container("native") is True

    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    assert runtime_env.is_container() is True


# --- Kubernetes identity -----------------------------------------------


_K8S = {"INSTALL_MODE": "kubernetes"}

_CHART_IDENTITY = {
    **_K8S,
    "CREMIND_K8S_NAMESPACE": "lee-cremind",
    "CREMIND_K8S_RELEASE": "cremind",
    "CREMIND_K8S_WORKLOAD": "cremind",
    "CREMIND_K8S_SERVICE_PORT": "80",
}


def _identity(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict | None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runtime_env.kubernetes_identity()


def test_identity_reads_what_the_chart_stated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the runbook stops printing <namespace> placeholders.

    ``port_forward`` is what the Helm NOTES print and what the user pastes, so
    it is pinned character for character rather than pattern-matched.
    """
    identity = _identity(monkeypatch, _CHART_IDENTITY)

    assert identity == {
        "namespace": "lee-cremind",
        "release": "cremind",
        "workload": "cremind",
        "service": "cremind",
        "service_port": 80,
        "source": "chart",
        "port_forward": (
            "kubectl --namespace lee-cremind port-forward svc/cremind 1515:80"
        ),
    }
    # An int, not the "80" string the environment carried: the config export
    # and the port-forward line both do arithmetic-free formatting with it, but
    # the UI compares it against numbers.
    assert isinstance(identity["service_port"], int)


def test_identity_follows_a_custom_service_port(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = _identity(
        monkeypatch, {**_CHART_IDENTITY, "CREMIND_K8S_SERVICE_PORT": "8080"}
    )

    assert identity["service_port"] == 8080
    assert identity["port_forward"].endswith("1515:8080")


def test_identity_is_inferred_when_the_chart_is_too_old(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A cluster upgrades the venv PVC in place, so this code meets old charts.

    The pod can still recover its namespace (the service-account file) and its
    Deployment name (the pod name minus the two generated suffixes), but never
    the Helm release - nothing in a pod carries it. ``source: inferred`` is what
    tells the runbook to keep ``helm list`` as its first command.
    """
    namespace_file = tmp_path / "namespace"
    namespace_file.write_text("lee-cremind\n", encoding="utf-8")
    monkeypatch.setattr(runtime_env, "_SA_NAMESPACE_FILE", namespace_file)

    identity = _identity(
        monkeypatch,
        {
            **_K8S,
            "KUBERNETES_SERVICE_HOST": "10.96.0.1",
            "HOSTNAME": "lee-cremind-5b8d7c9f8d-x2k9p",
        },
    )

    assert identity["namespace"] == "lee-cremind"
    assert identity["workload"] == "lee-cremind"
    assert identity["service"] == "lee-cremind"
    assert identity["release"] is None
    assert identity["source"] == "inferred"
    assert identity["port_forward"] == (
        "kubectl --namespace lee-cremind port-forward svc/lee-cremind 1515:80"
    )


def test_a_hostname_alone_is_never_a_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HOSTNAME`` exists on every machine; ``KUBERNETES_SERVICE_HOST`` doesn't.

    ``INSTALL_MODE=kubernetes`` can be typed into a local ``.env`` by hand, so
    it cannot be the only thing standing between a laptop's hostname and a
    Deployment name we print into a kubectl command.
    """
    identity = _identity(
        monkeypatch, {**_K8S, "HOSTNAME": "lee-cremind-5b8d7c9f8d-x2k9p"}
    )

    assert identity["workload"] is None
    assert identity["source"] is None


def test_a_pod_that_is_not_a_deployment_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A StatefulSet or bare pod name doesn't carry the two hash suffixes.

    Guessing there would print a kubectl line naming a Service that does not
    exist, which is worse than a placeholder the operator can see is a blank.
    """
    identity = _identity(
        monkeypatch,
        {**_K8S, "KUBERNETES_SERVICE_HOST": "10.96.0.1", "HOSTNAME": "cremind-0"},
    )

    assert identity["workload"] is None
    assert identity["port_forward"] is None


def test_identity_when_the_pod_can_name_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chart env, no service-account file, no pod name: every name unknown.

    The block still exists (consumers spread it unconditionally) and the
    Service port still defaults to the chart's 80, so a placeholder command can
    be rendered from it.
    """
    identity = _identity(monkeypatch, _K8S)

    assert identity["namespace"] is None
    assert identity["release"] is None
    assert identity["workload"] is None
    assert identity["service"] is None
    assert identity["source"] is None
    assert identity["port_forward"] is None
    assert identity["service_port"] == 80


@pytest.mark.parametrize(
    "namespace",
    [
        "lee cremind",          # a space would split the kubectl argument
        "lee-cremind\nrm -rf",  # tls_steps.command() raises on a newline
        "Lee-Cremind",          # DNS-1123 is lowercase only
        "-cremind",
        "x" * 64,
    ],
)
def test_a_name_that_is_not_a_kubernetes_name_is_not_printed(
    monkeypatch: pytest.MonkeyPatch, namespace: str,
) -> None:
    """These values come from ``cremind.extraEnv``, where anything is possible.

    A newline in one of them used to be enough to make ``tls_steps.command()``
    raise and turn ``/api/tls/status`` into a 500 - the page that was supposed
    to explain the switch. An unusable name reads as "the chart didn't say".
    """
    identity = _identity(
        monkeypatch, {**_CHART_IDENTITY, "CREMIND_K8S_NAMESPACE": namespace}
    )

    assert identity["namespace"] is None
    assert identity["port_forward"] is None
    # Still not "chart": one of the three names did not arrive intact.
    assert identity["source"] == "inferred"


@pytest.mark.parametrize("mode", ["docker", "native"])
def test_no_identity_off_kubernetes(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """``None``, not an empty block: every consumer keys off the whole thing."""
    env = {**_CHART_IDENTITY, "INSTALL_MODE": mode} if mode == "docker" else _CHART_IDENTITY
    if mode == "native":
        env = {key: value for key, value in env.items() if key != "INSTALL_MODE"}

    assert _identity(monkeypatch, env) is None
    assert _describe(monkeypatch, env)["kubernetes"] is None


def test_identity_rides_the_description(monkeypatch: pytest.MonkeyPatch) -> None:
    described = _describe(monkeypatch, _CHART_IDENTITY)

    assert described["kubernetes"] == runtime_env.kubernetes_identity("kubernetes")


def test_port_forward_command_is_one_pasteable_line() -> None:
    """The copy button hands this to a shell verbatim.

    Same contract ``app.config.tls_steps.command()`` enforces on the HTTPS
    runbook, asserted here because this module builds the line and the runbook
    only renders it.
    """
    from app.config.tls_steps import command

    line = runtime_env.kubernetes_port_forward("lee-cremind", "cremind", 1515, 80)

    assert line == "kubectl --namespace lee-cremind port-forward svc/cremind 1515:80"
    assert command(line)["text"] == line


# --- the VNC desktop descriptor ----------------------------------------


def _vnc(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict:
    return _describe(monkeypatch, env)["vnc"]


_DOCKER_DESKTOP = {"INSTALL_MODE": "docker", "CREMIND_IMAGE_FLAVOR": "desktop"}
_K8S_PROXY_URL = "http://localhost:1515/vnc/vnc.html"
_K8S_RELAY_URL = "http://localhost:6080/vnc.html"


@pytest.mark.parametrize(
    "env,label",
    [
        ({"ENV": "local"}, "native"),
        ({"INSTALL_MODE": "docker", "CREMIND_IMAGE_FLAVOR": "basic"}, "basic image"),
    ],
)
def test_no_desktop_no_descriptor(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], label: str,
) -> None:
    """Nothing to open, so nothing to describe - the card stays hidden."""
    vnc = _vnc(monkeypatch, env)

    assert vnc["enabled"] is False, label
    assert vnc["access"] is None
    assert vnc["novnc_path"] is None
    assert vnc["novnc_port"] is None
    assert vnc["novnc_url"] is None
    assert vnc["port_forward_commands"] == []
    assert vnc["scheme_note"] is None


def test_docker_publishes_novnc_on_its_own_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct access: websockify has a host port and Cremind's HTTPS misses it.

    ``novnc_url`` stays None on purpose - the host belongs to the browser (a
    Docker install is reached on whatever address the operator typed), so the
    SPA composes it from ``location.hostname`` and naming localhost here would
    be wrong for every remote tab.
    """
    vnc = _vnc(monkeypatch, _DOCKER_DESKTOP)

    assert vnc["enabled"] is True
    assert vnc["access"] == "direct"
    assert vnc["novnc_path"] == "/vnc.html"
    assert vnc["novnc_port"] == 6080
    assert vnc["novnc_url"] is None
    assert vnc["port_forward_commands"] == []
    assert "plain http" in vnc["scheme_note"]


def test_docker_honours_a_remapped_novnc_port(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _vnc(monkeypatch, {**_DOCKER_DESKTOP, "NOVNC_PORT": "7000"})["novnc_port"] == 7000


def test_docker_reads_the_port_from_the_compose_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``NOVNC_PORT`` is a Compose *project* variable, not a container one.

    docker-compose.yml.tmpl uses it in ``ports:`` only, so inside the container
    the env var is normally absent and the bind-mounted host ``.env`` is the
    only place the operator's choice survives.
    """
    env_file = tmp_path / "compose.env"
    env_file.write_text("# comment\nNOVNC_PORT=7001\nVNC_PORT=5900\n", encoding="utf-8")

    vnc = _vnc(
        monkeypatch,
        {**_DOCKER_DESKTOP, "CREMIND_COMPOSE_ENV_FILE": str(env_file)},
    )

    assert vnc["novnc_port"] == 7001


def test_docker_falls_back_to_the_installer_docker_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Host install: no container in front of the backend, no bind mount."""
    from app.config.settings import BaseConfig

    install_dir = tmp_path / "install"
    (install_dir / "docker").mkdir(parents=True)
    (install_dir / "docker" / ".env").write_text("NOVNC_PORT=7002\n", encoding="utf-8")
    monkeypatch.setattr(BaseConfig, "CREMIND_INSTALL_DIR", str(install_dir))

    assert _vnc(monkeypatch, _DOCKER_DESKTOP)["novnc_port"] == 7002


def test_kubernetes_proxy_serves_the_desktop_on_the_app_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sidecar fronts SPA, API and noVNC on one port, so it is just a path.

    Reached through a port-forward there is still a command to show, but only
    because the tunnel has to exist at all - it is the same one that carries
    Cremind, which is what the label has to say so nobody opens a second.
    """
    vnc = _vnc(
        monkeypatch, {**_CHART_IDENTITY, "CREMIND_NOVNC_URL": _K8S_PROXY_URL}
    )

    assert vnc["access"] == "same_origin"
    assert vnc["novnc_path"] == "/vnc/vnc.html"
    assert vnc["novnc_port"] is None
    assert vnc["novnc_url"] == _K8S_PROXY_URL
    assert len(vnc["port_forward_commands"]) == 1
    step = vnc["port_forward_commands"][0]
    assert step["command"] == (
        "kubectl --namespace lee-cremind port-forward svc/cremind 1515:80"
    )
    assert step["open_url"] == "http://localhost:1515/vnc/vnc.html"
    assert "same scheme" in vnc["scheme_note"]


def test_an_ingress_needs_no_tunnel_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behind an Ingress the public hostname is the whole answer."""
    vnc = _vnc(
        monkeypatch,
        {
            **_CHART_IDENTITY,
            "CREMIND_TLS_TERMINATION": "edge",
            "CREMIND_NOVNC_URL": "https://cremind.example/vnc/vnc.html",
        },
    )

    assert vnc["access"] == "same_origin"
    assert vnc["novnc_url"] == "https://cremind.example/vnc/vnc.html"
    assert vnc["port_forward_commands"] == []


def test_in_pod_tls_puts_the_desktop_behind_its_own_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With in-pod TLS the sidecar is an L4 relay and noVNC gets a Service port.

    No tunnel that already exists forwards it, so both ways of opening one ship
    with the descriptor: the desktop port alone, or one tunnel carrying Cremind
    and the desktop together.
    """
    vnc = _vnc(
        monkeypatch,
        {**_CHART_IDENTITY, "CREMIND_SSL": "auto", "CREMIND_NOVNC_URL": _K8S_RELAY_URL},
    )

    assert vnc["access"] == "port_forward"
    assert vnc["novnc_path"] == "/vnc.html"
    assert vnc["novnc_port"] == 6080
    assert vnc["novnc_url"] == _K8S_RELAY_URL
    commands = [step["command"] for step in vnc["port_forward_commands"]]
    assert commands == [
        "kubectl --namespace lee-cremind port-forward svc/cremind 6080:6080",
        "kubectl --namespace lee-cremind port-forward svc/cremind 1515:80 6080:6080",
    ]
    assert all(step["open_url"] == _K8S_RELAY_URL for step in vnc["port_forward_commands"])
    assert "tunnel that port first" in vnc["scheme_note"]


@pytest.mark.parametrize(
    "novnc_url,access,path",
    [
        ("http://localhost:1515/vnc/vnc.html", "same_origin", "/vnc/vnc.html"),
        ("https://cremind.example/vnc/vnc.html", "same_origin", "/vnc/vnc.html"),
        ("https://cremind.example//vnc/vnc.html", "same_origin", "/vnc/vnc.html"),
        ("https://cremind.example/vnc/", "same_origin", "/vnc/"),
        (
            "https://cremind.example/cremind/vnc/vnc.html",
            "same_origin",
            "/cremind/vnc/vnc.html",
        ),
        # Directory form under a sub-path: nginx's ``location = /vnc`` redirects
        # to vnc.html, so an operator may well have stored the route this way.
        ("https://cremind.example/cremind/vnc/", "same_origin", "/cremind/vnc/"),
        # noVNC ships more than one page (vnc_lite.html is the stripped-down
        # viewer), so the page name cannot be what identifies the route.
        (
            "https://cremind.example/cremind/vnc/vnc_lite.html",
            "same_origin",
            "/cremind/vnc/vnc_lite.html",
        ),
        (
            "https://cremind.example/cremind/vnc/vnc.html/",
            "same_origin",
            "/cremind/vnc/vnc.html/",
        ),
        (
            "https://cremind.example/cremind//vnc/vnc.html",
            "same_origin",
            "/cremind/vnc/vnc.html",
        ),
        ("http://localhost:6080/vnc.html", "port_forward", "/vnc.html"),
    ],
    ids=[
        "bare-origin",
        "ingress-host",
        "doubled-slash",
        "trailing-slash",
        "sub-path",
        "sub-path-directory",
        "sub-path-other-page",
        "sub-path-trailing-slash",
        "sub-path-doubled-slash",
        "relay",
    ],
)
def test_the_proxy_shape_survives_an_appurl_that_is_not_a_bare_origin(
    monkeypatch: pytest.MonkeyPatch, novnc_url: str, access: str, path: str,
) -> None:
    """``cremind.appUrl`` is an operator string, and the chart concatenates it.

    ``CREMIND_NOVNC_URL`` is rendered as ``<appUrl>/vnc/vnc.html``, so an
    appUrl with a trailing slash yields ``//vnc/vnc.html`` and one with a
    sub-path yields ``/cremind/vnc/vnc.html``. Neither starts with ``/vnc/``,
    which is all the discriminator used to look at - so a proxy-sidecar install
    was reported as the in-pod-TLS relay, and the desktop card told the user to
    open two ``kubectl port-forward`` tunnels to Service port 6080. That
    Service does not publish 6080 at all without in-pod TLS: both commands fail
    with "Service does not have a service port 6080".

    Matching the page name instead left the same hole one step further in: a
    sub-path install whose stored URL is the directory (``/cremind/vnc/``),
    another noVNC page (``vnc_lite.html``) or carries a trailing slash still
    failed both halves of the test and got the relay's tunnels. What names the
    sidecar route is the ``/vnc/`` *segment*, wherever in the path it sits -
    the chart hangs it off appUrl, so anything before it is the prefix an
    Ingress adds.

    The chart no longer emits the doubled slash (see
    ``tests/install/test_helm_ssl.py``), but an install already running keeps
    the value its own render stored, so this end has to classify it too.
    """
    vnc = _vnc(monkeypatch, {**_CHART_IDENTITY, "CREMIND_NOVNC_URL": novnc_url})

    assert vnc["access"] == access
    assert vnc["novnc_path"] == path
    if access == "same_origin":
        assert vnc["novnc_port"] is None
        # The relay's Service port must not appear anywhere on a proxy install:
        # that is the port the misclassification told people to tunnel.
        assert all(
            "6080" not in step["command"] for step in vnc["port_forward_commands"]
        )


@pytest.mark.parametrize(
    "novnc_url,novnc_path,tunnel_url",
    [
        (
            "https://cremind.example/cremind/vnc/vnc.html",
            "/cremind/vnc/vnc.html",
            "http://localhost:1515/vnc/vnc.html",
        ),
        (
            "https://cremind.example/cremind/vnc/",
            "/cremind/vnc/",
            "http://localhost:1515/vnc/",
        ),
        # An appUrl that itself ends in a ``vnc`` segment: the route the chart
        # appended is the last one, so everything before it is still prefix.
        (
            "https://cremind.example/vnc/vnc/vnc.html",
            "/vnc/vnc/vnc.html",
            "http://localhost:1515/vnc/vnc.html",
        ),
        # No prefix to drop: the public path already is the in-pod route.
        (_K8S_PROXY_URL, "/vnc/vnc.html", "http://localhost:1515/vnc/vnc.html"),
    ],
    ids=["sub-path", "sub-path-directory", "appurl-ends-in-vnc", "bare-origin"],
)
def test_the_tunnel_link_is_the_in_pod_route_not_the_public_path(
    monkeypatch: pytest.MonkeyPatch,
    novnc_url: str,
    novnc_path: str,
    tunnel_url: str,
) -> None:
    """A port-forward lands on the sidecar, where the sub-path does not exist.

    An Ingress serving Cremind under ``/cremind`` makes the desktop public at
    ``/cremind/vnc/vnc.html``, but the tunnel skips the Ingress entirely and
    talks to the sidecar's own listener - and the sidecar knows exactly one
    noVNC route, ``location /vnc/`` (helm/cremind/templates/proxy-configmap.yaml).
    Handing the public path to a tunnelled localhost therefore 404s through the
    SPA fallback. Only this link changes: ``novnc_path`` stays the public one
    because the SPA composes it against the origin the browser actually used.
    """
    vnc = _vnc(monkeypatch, {**_CHART_IDENTITY, "CREMIND_NOVNC_URL": novnc_url})

    assert vnc["access"] == "same_origin"
    assert vnc["novnc_path"] == novnc_path
    assert vnc["novnc_url"] == novnc_url
    assert [step["open_url"] for step in vnc["port_forward_commands"]] == [tunnel_url]


@pytest.mark.parametrize(
    "ssl,access",
    [("auto", "port_forward"), ("after-setup", "port_forward"), ("", "same_origin")],
)
def test_an_older_chart_states_no_novnc_url(
    monkeypatch: pytest.MonkeyPatch, ssl: str, access: str,
) -> None:
    """Without the variable, in-pod TLS is the signal that stands in for it.

    ``CREMIND_NOVNC_URL`` arrived with the flavour split; a pod on an older
    chart running this code has to be classified anyway, and in-pod TLS is the
    only reason the sidecar stops proxying noVNC.
    """
    env = {**_CHART_IDENTITY}
    if ssl:
        env["CREMIND_SSL"] = ssl
    vnc = _vnc(monkeypatch, env)

    assert vnc["access"] == access
    assert vnc["novnc_path"] == (
        "/vnc.html" if access == "port_forward" else "/vnc/vnc.html"
    )
    # The fallback has to fill in the whole descriptor, not just the shape: the
    # relay branch composes its own link (nothing stated one), the proxy branch
    # has no port of its own because it is a path on the app origin.
    if access == "port_forward":
        assert vnc["novnc_port"] == 6080
        assert vnc["novnc_url"] == _K8S_RELAY_URL
    else:
        assert vnc["novnc_port"] is None
        assert vnc["novnc_url"] is None


def test_the_commands_carry_this_cluster_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity is what turns the runbook from a template into a command."""
    vnc = _vnc(
        monkeypatch,
        {
            "INSTALL_MODE": "kubernetes",
            "CREMIND_K8S_NAMESPACE": "team-a",
            "CREMIND_K8S_RELEASE": "cremind-prod",
            "CREMIND_K8S_WORKLOAD": "cremind-prod",
            "CREMIND_K8S_SERVICE_PORT": "8080",
            "CREMIND_NOVNC_URL": _K8S_PROXY_URL,
        },
    )

    assert vnc["port_forward_commands"][0]["command"] == (
        "kubectl --namespace team-a port-forward svc/cremind-prod 1515:8080"
    )


def test_an_unnamed_pod_shows_the_runbook_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same blanks the HTTPS runbook prints, so one install speaks one language.

    ``<release>`` stands in for the workload because that is what the operator
    types into ``helm upgrade``, and ``helm list`` is how they find it.
    """
    vnc = _vnc(monkeypatch, {**_K8S, "CREMIND_NOVNC_URL": _K8S_PROXY_URL})

    assert vnc["port_forward_commands"][0]["command"] == (
        "kubectl --namespace <namespace> port-forward svc/<release> 1515:80"
    )


@pytest.mark.parametrize(
    "env",
    [
        {**_CHART_IDENTITY, "CREMIND_NOVNC_URL": _K8S_PROXY_URL},
        {**_CHART_IDENTITY, "CREMIND_NOVNC_URL": _K8S_RELAY_URL},
        {**_K8S, "CREMIND_NOVNC_URL": _K8S_RELAY_URL},
        {**_K8S, "CREMIND_SSL": "auto"},
        {**_K8S},
    ],
    ids=["proxy", "relay", "relay-unnamed", "old-chart-ssl", "old-chart-plain"],
)
def test_every_published_command_is_pasteable(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str],
) -> None:
    """One bare line each, or the copy button hands prose to a shell.

    The UI renders these through the same component as the HTTPS runbook, and
    that component gives *only* commands a copy button - so they go through the
    very validator the runbook's own steps do.
    """
    from app.config.tls_steps import command

    described = _describe(monkeypatch, env)
    lines = [step["command"] for step in described["vnc"]["port_forward_commands"]]
    if described["kubernetes"] and described["kubernetes"]["port_forward"]:
        lines.append(described["kubernetes"]["port_forward"])

    assert lines
    for line in lines:
        assert command(line)["text"] == line


def test_the_prompt_line_ignores_the_new_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The agent's line must stay byte-identical for every install it ever was.

    It sits in a prompt-cached system message, so adding cluster names, a
    port-forward command or a CPU fingerprint to it would cost a cache miss on
    every turn for every Kubernetes user - and the agent can ask ``cremind
    server environment`` for any of it. Compared against the same install
    *without* the identity so the only difference under test is the new
    environment.

    The CPU block is here rather than in its own case because it is the same
    invariant and the same cost: the ``cpu`` facts are read once per process
    like the rest of the cached description, and a tool refusing to run on this
    host says so in its own result, not in a system prompt every profile
    shares.
    """
    _describe(monkeypatch, _K8S)
    before = runtime_env.runtime_environment_prompt_line()

    _cpuinfo(monkeypatch, tmp_path, _QEMU64_CPUINFO)
    described = _describe(
        monkeypatch, {**_CHART_IDENTITY, "CREMIND_NOVNC_URL": _K8S_RELAY_URL}
    )

    assert described["kubernetes"]["source"] == "chart"
    assert described["cpu"]["x86_64_level"] == "v1"
    assert runtime_env.runtime_environment_prompt_line() == before
    line = runtime_env.runtime_environment_prompt_line()
    assert "6080" not in line
    assert "port-forward" not in line
    assert "lee-cremind" not in line
    assert "QEMU" not in line
    assert "sse4_1" not in line
    assert "x86_64" not in line


# --- the CPU underneath ------------------------------------------------
#
# Every row here exists because a prebuilt binary Cremind ships (the Claude Code
# CLI, a Bun single-file executable built for x86-64-v2) does not fail on a CPU
# that hides those instructions - it spins at 100% CPU forever, writing no log.
# The probe that turns that into a fast refusal must therefore be exactly right
# in one direction: a *false* "missing" blocks every host where the binary works
# fine, which is far worse than the slow hang it was meant to prevent. So the
# cases below spend most of their weight on the ways the answer must come back
# "could not tell".


# The pod's own flag line, quoted from /proc/cpuinfo on the Kubernetes node this
# work was written for: a QEMU virtual CPU with no ssse3, sse4_1, sse4_2 or
# popcnt at all - x86-64-v1, in 2026.
_QEMU64_CPUINFO = """processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 6
model\t\t: 6
model name\t: QEMU Virtual CPU version 2.5+
stepping\t: 3
microcode\t: 0x1000065
cpu MHz\t\t: 2799.998
flags\t\t: apic clflush cmov constant_tsc cpuid cpuid_fault cx16 cx8 de fpu \
fxsr hypervisor lahf_lm lm mca mce mmx msr mtrr nopl nx pae pat pge pni pse \
pse36 pti sep sse sse2 syscall tsc tsc_known_freq x2apic xtopology
bugs\t\t: null_seg
"""

# A physical CPU of any recent generation: everything the probe looks for, so
# ``missing`` comes back empty and the level reads v3.
_FULL_HOST_CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat \
pse36 clflush mmx fxsr sse sse2 ss ht syscall nx pdpe1gb rdtscp lm pni \
pclmulqdq ssse3 fma cx16 sse4_1 sse4_2 movbe popcnt aes xsave avx f16c rdrand \
lahf_lm abm 3dnowprefetch bmi1 avx2 bmi2 erms rdseed adx clflushopt
"""

# What an ARM box writes instead. There is no ``flags:`` line anywhere in it -
# the key is ``Features`` - and the names underneath share no vocabulary with
# x86 at all.
_AARCH64_CPUINFO = """processor\t: 0
BogoMIPS\t: 48.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp \
cpuid asimdrdm jscvt fcma lrcpc dcpop sha3 sm3 sm4 asimddp sha512 asimdfhm
CPU implementer\t: 0x41
CPU part\t: 0xd0c
"""


def _cpuinfo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str,
    *,
    machine: str = "x86_64",
    system: str = "Linux",
) -> dict:
    """Answer from ``text`` as if it were this host's /proc/cpuinfo.

    Both halves have to be pinned together: the probe only parses the file at
    all on Linux/x86_64, so a suite running on Windows or an ARM mac would get
    "unknown" for every case here no matter what the file said.
    """
    path = tmp_path / "cpuinfo"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(runtime_env, "_CPUINFO_PATH", path)
    monkeypatch.setattr(runtime_env, "_host_platform", lambda: (system, machine))
    runtime_env.describe_runtime_environment.cache_clear()
    return runtime_env.cpu_features()


def test_a_qemu_virtual_cpu_reports_what_it_cannot_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The node that started all this, and the only row that must say "no".

    ``hypervisor`` is what turns the finding into an action - the flags are
    absent because the host handed the VM a generic CPU model, which is a
    setting on the hypervisor and not anything Cremind can change - and the four
    v2 flags lead ``missing`` because they are the half that decides whether a
    baseline-built binary runs at all.
    """
    cpu = _cpuinfo(monkeypatch, tmp_path, _QEMU64_CPUINFO)

    assert cpu["flags_known"] is True
    assert cpu["arch"] == "x86_64"
    assert cpu["model"] == "QEMU Virtual CPU version 2.5+"
    assert cpu["hypervisor"] is True
    assert cpu["missing"][:4] == list(runtime_env.X86_64_V2_FLAGS)
    assert cpu["x86_64_level"] == "v1"
    # It advertises nothing this probe looks for, v3 included.
    assert cpu["present"] == []
    assert set(cpu["missing"]) == set(
        runtime_env.X86_64_V2_FLAGS + runtime_env.X86_64_V3_FLAGS
    )


def test_a_full_featured_host_is_missing_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The other end, and the one that must never grow a false positive.

    An empty ``missing`` is what every consumer reads as "let it run", so the
    flag names have to match /proc/cpuinfo's own spelling exactly - notably
    ``abm``, which is how Linux reports LZCNT and the reason a probe looking for
    ``lzcnt`` would call this CPU incapable.
    """
    cpu = _cpuinfo(monkeypatch, tmp_path, _FULL_HOST_CPUINFO)

    assert cpu["flags_known"] is True
    assert cpu["missing"] == []
    assert cpu["x86_64_level"] == "v3"
    assert cpu["hypervisor"] is False
    assert cpu["present"] == list(
        runtime_env.X86_64_V2_FLAGS + runtime_env.X86_64_V3_FLAGS
    )


def test_a_v2_only_cpu_is_not_reported_as_v3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A modest but perfectly working CPU: v2 whole, v3 partial.

    Nothing we run needs v3, so this host must read as capable - the level says
    ``v2`` and the v3 names in ``missing`` are diagnosis, not a verdict. The
    level cannot be derived from "is anything missing?" for exactly this reason.
    """
    trimmed = _FULL_HOST_CPUINFO.replace(" avx2 ", " ").replace(" bmi2 ", " ")
    cpu = _cpuinfo(monkeypatch, tmp_path, trimmed)

    assert cpu["x86_64_level"] == "v2"
    assert cpu["missing"] == ["avx2", "bmi2"]
    assert all(flag in cpu["present"] for flag in runtime_env.X86_64_V2_FLAGS)


def test_off_linux_x86_nothing_is_claimed_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A mac has no /proc at all, and would answer "everything is missing".

    The whole set therefore reads as unknown rather than absent: ``flags_known``
    False, the two lists empty and the level ``None``. ``arch`` survives because
    it is the one fact ``platform`` gives us everywhere.
    """
    cpu = _cpuinfo(
        monkeypatch, tmp_path, _FULL_HOST_CPUINFO, system="Darwin", machine="arm64",
    )

    assert cpu["flags_known"] is False
    assert cpu["arch"] == "arm64"
    assert cpu["model"] is None
    assert cpu["hypervisor"] is None
    assert cpu["x86_64_level"] is None
    assert cpu["present"] == [] and cpu["missing"] == []


@pytest.mark.parametrize("machine", ["aarch64", "x86_64"])
def test_an_arm_feature_line_is_not_an_x86_flag_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: str,
) -> None:
    """The false positive that would block a working host, tested from both ends.

    aarch64 spells the line ``Features:``, and its names share no vocabulary
    with x86 - so a probe that accepted any flag-ish line would find none of
    ``ssse3``/``sse4_1``/``sse4_2``/``popcnt``, report the full v2 set as
    missing and refuse to run the coding agents on a machine where they are
    fine. Both gates are asserted independently: the architecture (an ARM host
    is never parsed) and the key itself (a file with no ``flags:`` line yields
    nothing even where the architecture says x86).
    """
    cpu = _cpuinfo(monkeypatch, tmp_path, _AARCH64_CPUINFO, machine=machine)

    assert cpu["flags_known"] is False, machine
    assert cpu["missing"] == []
    assert cpu["x86_64_level"] is None


def test_an_unreadable_cpuinfo_is_never_an_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """This block rides a listing endpoint and a tool catalogue.

    A kernel that mounts /proc differently, a container that hides it, a
    directory where a file was expected - none of those may turn
    ``/api/system/environment`` or a tool's own status into a 500, so the probe
    swallows everything and degrades to "could not tell".
    """
    monkeypatch.setattr(runtime_env, "_host_platform", lambda: ("Linux", "x86_64"))

    for path in (tmp_path / "absent", tmp_path):  # missing file, then a directory
        monkeypatch.setattr(runtime_env, "_CPUINFO_PATH", path)
        runtime_env.describe_runtime_environment.cache_clear()
        cpu = runtime_env.cpu_features()
        assert cpu["flags_known"] is False, path
        assert cpu["arch"] == "x86_64"
        assert cpu["model"] is None and cpu["x86_64_level"] is None


def test_the_cpu_block_rides_the_description(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One probe, one answer, wherever it is read from.

    ``/api/system/environment`` and ``cremind server environment`` see it
    through the description; the tool runners call ``cpu_features()`` straight.
    A second copy of the parsing would be a second verdict about one CPU.
    """
    _cpuinfo(monkeypatch, tmp_path, _QEMU64_CPUINFO)
    described = _describe(monkeypatch, {"INSTALL_MODE": "kubernetes"})

    assert described["cpu"] == runtime_env.cpu_features()
    assert described["cpu"]["model"] == "QEMU Virtual CPU version 2.5+"


def test_cache_clear_resets_the_cpu_probe_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``cache_clear`` on the description has to clear both caches now.

    Dozens of fixtures call it as their one "forget this machine" handle. The
    CPU facts live in a cache of their own because the tool runners ask for them
    without a description in hand, so clearing only the outer one would freeze
    the ``cpu`` block at whatever the first case in a file wrote - which is a
    test that passes alone and fails in the suite.
    """
    _cpuinfo(monkeypatch, tmp_path, _QEMU64_CPUINFO)
    assert _describe(monkeypatch, {})["cpu"]["x86_64_level"] == "v1"

    (tmp_path / "cpuinfo").write_text(_FULL_HOST_CPUINFO, encoding="utf-8")
    assert runtime_env.cpu_features()["x86_64_level"] == "v1", "still cached"

    runtime_env.describe_runtime_environment.cache_clear()

    assert runtime_env.cpu_features()["x86_64_level"] == "v3"
    assert runtime_env.describe_runtime_environment()["cpu"]["missing"] == []


# --- the unauthenticated subset ----------------------------------------


def test_public_descriptor_keeps_cluster_topology_out_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/api/features/tray-capabilities`` answers before anyone has logged in.

    It exists so the desktop shell can decide whether a "VNC desktop" menu
    entry belongs there, which needs the shape and the port and nothing else -
    namespaces, Service names and ready-to-run kubectl lines stay admin-only.
    """
    vnc = _vnc(
        monkeypatch,
        {**_CHART_IDENTITY, "CREMIND_SSL": "auto", "CREMIND_NOVNC_URL": _K8S_RELAY_URL},
    )

    public = runtime_env.public_vnc_descriptor(vnc)

    assert public == {
        "enabled": True,
        "access": "port_forward",
        "novnc_path": "/vnc.html",
        "novnc_port": 6080,
    }


def test_public_descriptor_of_a_disabled_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    public = runtime_env.public_vnc_descriptor(_vnc(monkeypatch, {"ENV": "local"}))

    assert public == {
        "enabled": False,
        "access": None,
        "novnc_path": None,
        "novnc_port": None,
    }
