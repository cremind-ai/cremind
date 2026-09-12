"""Every reader of the install mode must tell one story about one machine.

Three modules used to answer "what is running us?" separately — the HTTPS path
(``tls_managed_env``), the Developer page and the agent's prompt line
(``runtime_env``), and the shutdown path (``server``) — each with its own copy
of the rule and its own drift. The drift was not theoretical: a Compose
container whose environment claimed ``INSTALL_MODE=native`` was "Docker,
supervised" on the Developer page and "nothing supervises this server — press
Ctrl+C" on the HTTPS page, which is the bug this file exists to make
unrepeatable. There is now one resolver and this is the test that says so.

The grid is every shape the signals can take, and ``expected`` is computed from
the rule in ``tls_managed_env.resolve_install_mode`` restated here by hand — a
test that called the implementation to decide what to expect would agree with
any future mistake.
"""

from __future__ import annotations

import pytest

from app import server
from app.api import llm_codex_flow
from app.config import runtime_env
from app.config import tls_managed_env as managed
from app.config.tls_managed_env import (
    INSTALL_MODE_INFERRED,
    INSTALL_MODE_OVERRIDDEN,
    INSTALL_MODE_SAID,
)
from app.config.tls_mode import current_tls_facts
from app.config.tls_transition import management

CLAIMS = [None, "docker", "kubernetes", "native", "custom", "Docker", "podman"]
PODS = [None, "env", "serviceaccount"]


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """One machine, seen through every module's own patchable marker.

    Each reader keeps its own ``_CONTAINER_MARKER`` so it stays independently
    neutralisable (CI may itself run in a container); pointing them all at one
    path here is what makes "the same machine" meaningful.
    """
    marker = tmp_path / "dockerenv"
    absent = tmp_path / "no-dockerenv"
    for module in (managed, runtime_env, server, llm_codex_flow):
        monkeypatch.setattr(module, "_CONTAINER_MARKER", absent)
    monkeypatch.setattr(managed, "_POD_MARKER", tmp_path / "no-serviceaccount")
    for key in ("INSTALL_MODE", "VNC_PASSWORD", "KUBERNETES_SERVICE_HOST",
                "CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED", "CREMIND_TLS_TERMINATION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CREMIND_UI_PORT", "1515")
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path))

    def configure(claim, in_container, vnc, pod):
        if claim is not None:
            monkeypatch.setenv("INSTALL_MODE", claim)
        if in_container:
            marker.write_text("", encoding="utf-8")
            for module in (managed, runtime_env, server, llm_codex_flow):
                monkeypatch.setattr(module, "_CONTAINER_MARKER", marker)
        if vnc:
            monkeypatch.setenv("VNC_PASSWORD", "changeme")
        if pod == "env":
            monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
        elif pod == "serviceaccount":
            sa = tmp_path / "serviceaccount"
            sa.mkdir(exist_ok=True)
            monkeypatch.setattr(managed, "_POD_MARKER", sa)
        runtime_env.describe_runtime_environment.cache_clear()

    return configure


def _expected(claim, in_container, vnc, pod) -> tuple[str, str]:
    """The rule, restated: (mode, provenance)."""
    normalised = (claim or "").strip().lower()
    if normalised not in managed.KNOWN_INSTALL_MODES:
        normalised = ""
    if normalised in ("docker", "kubernetes"):
        return normalised, INSTALL_MODE_SAID
    if pod:
        container = "kubernetes"
    elif vnc or in_container:
        container = "docker"
    else:
        container = ""
    if not container:
        return normalised, INSTALL_MODE_SAID
    return container, (
        INSTALL_MODE_OVERRIDDEN if normalised in ("native", "custom") else INSTALL_MODE_INFERRED
    )


@pytest.mark.parametrize("claim", CLAIMS)
@pytest.mark.parametrize("in_container", [False, True])
@pytest.mark.parametrize("vnc", [False, True])
@pytest.mark.parametrize("pod", PODS)
def test_every_reader_of_the_install_mode_tells_one_story(
    machine, claim, in_container, vnc, pod,
):
    machine(claim, in_container, vnc, pod)
    mode, provenance = _expected(claim, in_container, vnc, pod)
    container_mode = mode in ("docker", "kubernetes")

    # The resolver itself, and the provenance the boot log keys on.
    assert managed.effective_install_mode() == mode
    assert managed.install_mode_provenance() == provenance
    assert managed.is_container_install() is (mode == "docker")

    # The two readers that speak "native" for everything that is not a
    # container, and the Codex OAuth flow that resolves through one of them.
    assert runtime_env.detect_install_mode() == (mode if container_mode else "native")
    assert llm_codex_flow._detect_deployment() == (mode if container_mode else "native")
    assert runtime_env.is_container() is container_mode

    # "Will something restart us?" — asked by the shutdown path, the Developer
    # page, and the HTTPS page, which disagreed for the reported container.
    assert server._supervised_env() is container_mode
    assert runtime_env.supervised() is container_mode
    assert current_tls_facts(public_port=1515).restart_supported is container_mode

    # And who applies an HTTPS switch, which is what the user sees.
    assert management() == {
        "docker": "managed-docker", "kubernetes": "external",
    }.get(mode, "native")


def test_the_reported_container_is_docker_everywhere(machine):
    """The exact configuration from the report, spelled out rather than
    parametrised, so a regression names itself."""
    machine("native", in_container=True, vnc=False, pod=None)

    assert managed.effective_install_mode() == "docker"
    assert managed.install_mode_provenance() == INSTALL_MODE_OVERRIDDEN
    assert runtime_env.detect_install_mode() == "docker"
    assert server._supervised_env() is True
    assert current_tls_facts(public_port=1515).restart_supported is True
    assert management() == "managed-docker"
