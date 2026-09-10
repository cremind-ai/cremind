"""Tests for /api/system/environment - the admin view of this install.

The endpoint is a thin spread of :func:`describe_runtime_environment` plus two
per-request fields, so most of what it says has its own unit tests in
``tests/config/test_runtime_env.py``. What cannot be tested there is the thing
this module exists for: the Kubernetes identity now travels through *three*
doors - this endpoint (the Developer page's Environment card and ``cremind
server environment``), ``/api/config/install-secrets`` (the Setup Wizard, which
runs before an admin token exists and never calls this one) and the function
itself (``app/api/tls.py`` and the Codex sign-in hint call straight through).
Three copies of one contract is exactly the shape that drifts, and a drifted
copy is a ``kubectl`` line that names the wrong Service, so the agreement is
asserted as a pair rather than trusted per side.

The request stub and the uncached-description fixture are the ones from
``tests/api/test_features_capabilities.py``; that module owns the tray
descriptor's half of the same pairing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api import config as config_api
from app.api import system as system_api
from app.config import runtime_env


# The environment variables the shared description reads. A dev box running a
# real install has several of them set, and a CI runner inside a cluster would
# hand these tests that cluster's own namespace.
_SCRUBBED_ENV = (
    "CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED", "VNC_PASSWORD",
    "CREMIND_IMAGE_FLAVOR", "CREMIND_NOVNC_URL", "NOVNC_PORT", "CREMIND_SSL",
    "CREMIND_TLS_TERMINATION", "CREMIND_COMPOSE_ENV_FILE", "APP_URL",
    "RESOLUTION", "CORS_ALLOWED_ORIGINS", "SETUP_WIZARD_ENV",
    "CREMIND_K8S_NAMESPACE", "CREMIND_K8S_RELEASE", "CREMIND_K8S_WORKLOAD",
    "CREMIND_K8S_SERVICE_PORT", "KUBERNETES_SERVICE_HOST", "HOSTNAME",
)


# The flag line the Kubernetes node this endpoint was extended for really
# reports: a QEMU virtual CPU advertising none of the x86-64-v2 instructions the
# prebuilt coding-agent binaries are compiled for.
_QEMU64_CPUINFO = (
    "model name\t: QEMU Virtual CPU version 2.5+\n"
    "flags\t\t: apic clflush cmov constant_tsc cpuid cpuid_fault cx16 cx8 de "
    "fpu fxsr hypervisor lahf_lm lm mca mce mmx msr mtrr nopl nx pae pat pge "
    "pni pse pse36 pti sep sse sse2 syscall tsc tsc_known_freq x2apic "
    "xtopology\n"
)


@pytest.fixture(autouse=True)
def _uncached_runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """One case's install must not describe the next one's.

    The description is lru_cached for the life of the process because it feeds
    the agent's prompt-cached system prompt. Two of the three file probes are
    pointed at paths that cannot exist: ``/.dockerenv`` would make every native
    row here a Docker one on a containerised CI runner, and the service-account
    namespace file would name a real cluster.

    ``/proc/cpuinfo`` is pinned the other way - at a file this fixture writes,
    with the host platform forced to the one shape the probe parses - because
    the body is asserted to *carry* a CPU model and a missing-flag list. Left
    alone it would answer from the runner's own hardware: nothing at all on a
    Windows or macOS dev box, and whatever flags the build machine happens to
    have on Linux.
    """
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(_QEMU64_CPUINFO, encoding="utf-8")
    monkeypatch.setattr(runtime_env, "_CONTAINER_MARKER", tmp_path / "no-dockerenv")
    monkeypatch.setattr(runtime_env, "_SA_NAMESPACE_FILE", tmp_path / "no-namespace")
    monkeypatch.setattr(runtime_env, "_CPUINFO_PATH", cpuinfo)
    monkeypatch.setattr(runtime_env, "_host_platform", lambda: ("Linux", "x86_64"))
    runtime_env.describe_runtime_environment.cache_clear()
    yield
    runtime_env.describe_runtime_environment.cache_clear()


def _pin_install(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("INSTALL_MODE", mode)
    for name in _SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)


def _chart_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """What ``helm/cremind/templates/configmap.yaml`` states about the release."""
    monkeypatch.setenv("CREMIND_K8S_NAMESPACE", "lee-cremind")
    monkeypatch.setenv("CREMIND_K8S_RELEASE", "cremind")
    monkeypatch.setenv("CREMIND_K8S_WORKLOAD", "cremind")
    monkeypatch.setenv("CREMIND_K8S_SERVICE_PORT", "80")


def _environment() -> dict:
    """The admin body, as the Environment card and ``server environment`` see it."""
    request = SimpleNamespace(
        headers={}, cookies={}, client=None,
        user=SimpleNamespace(is_authenticated=True, username="admin"),
    )
    response = asyncio.run(system_api.get_system_environment(request))
    return json.loads(response.body)


def _install_secrets_handler() -> Callable:
    """Pull ``handle_install_secrets`` out of the ``get_config_routes`` closure."""
    state = SimpleNamespace(storage_ready=False)
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    for route in routes:
        if route.path == "/api/config/install-secrets":
            return route.endpoint
    raise AssertionError("install-secrets route not registered")


def _install_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> dict:
    """The wizard's body, with the on-disk sources pointed at an empty tree.

    A pod has neither a compose file nor an install bundle, which is the shape
    that matters here - the identity has to arrive from the environment alone.
    """
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)
    monkeypatch.setattr(
        config_api.BaseConfig, "CREMIND_INSTALL_DIR", str(tmp_path / "install"),
        raising=False,
    )
    monkeypatch.setattr(
        config_api.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path / "system"),
        raising=False,
    )
    request = SimpleNamespace(headers={}, cookies={})
    response = asyncio.run(_install_secrets_handler()(request))
    return json.loads(response.body)


# -- the identity contract, asserted as a pair ----------------------------


def test_one_identity_reaches_all_three_of_its_consumers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Same pod, same block, whichever door it comes out of.

    The Developer page reads this endpoint, the Setup Wizard reads
    install-secrets, and the HTTPS runbook calls ``kubernetes_identity``
    directly. A field that existed in one and not the others would show up as
    a runbook printing ``<namespace>`` on a machine whose Environment card was
    naming it two clicks away.
    """
    _pin_install(monkeypatch, "kubernetes")
    _chart_identity_env(monkeypatch)

    direct = runtime_env.kubernetes_identity("kubernetes")
    environment = _environment()["kubernetes"]
    wizard = _install_secrets(monkeypatch, tmp_path)["kubernetes"]

    assert environment == wizard == direct
    assert environment == {
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


def test_a_container_is_not_a_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Docker gets ``None``, not a half-filled guess.

    Both endpoints have to agree about that too: a rendered kubectl command on
    a Docker install is a command that cannot work, and the exported config
    file would print it under a ``## Kubernetes`` heading.
    """
    _pin_install(monkeypatch, "docker")
    # Even with the chart's keys somehow present in this process's env.
    _chart_identity_env(monkeypatch)

    assert _environment()["kubernetes"] is None
    assert _install_secrets(monkeypatch, tmp_path)["kubernetes"] is None


def test_a_native_install_has_no_identity_and_no_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_install(monkeypatch, "native")

    body = _environment()

    assert body["kubernetes"] is None
    assert body["vnc"] == {
        "enabled": False,
        "access": None,
        "novnc_path": None,
        "novnc_port": None,
        "novnc_url": None,
        "port_forward_commands": [],
        "scheme_note": None,
    }


# -- the fields that were already there -----------------------------------


def test_the_existing_keys_survive_the_two_new_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kubernetes`` and ``vnc`` are additions, not a re-shaping.

    The Developer page's config re-download reads
    ``deployment_custom_fields`` to re-render the wizard's export long after
    setup, and the Environment card reads ``effective_timezone`` - the
    per-profile answer resolved here rather than in the shared description.
    Both are added on top of the description by this handler, so a change to
    how that description is copied is exactly what would drop them.
    """
    from app.config import timezone as timezone_config

    _pin_install(monkeypatch, "native")
    monkeypatch.delenv("CREMIND_TIMEZONE", raising=False)
    monkeypatch.setattr(
        timezone_config, "get_dynamic",
        lambda _t, _k, profile=None: "Asia/Tokyo" if profile == "admin" else None,
    )

    body = _environment()

    assert body["effective_timezone"] == "Asia/Tokyo"
    assert set(body["deployment_custom_fields"]) == {
        "listen_host", "public_url", "allowed_origins", "wizard_preset",
    }
    # The description's own fields are still spread in beside them.
    assert body["install_mode"] == "native"
    assert "release_channel" in body and "system_dir" in body


def test_the_cpu_facts_reach_the_admin_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one door the CPU fingerprint travels through, and it is admin-only.

    A pod whose node hides the x86-64-v2 instructions makes the bundled Claude
    Code CLI spin at 100% CPU forever rather than fail, and the remedy is a
    hypervisor setting the operator may not even control - so the model name and
    the missing flags have to be *readable*, on the Developer page and in
    ``cremind server environment``, or the only symptom is "the agent hangs".
    The unauthenticated tray endpoint deliberately carries none of it; that half
    of the pairing is pinned in ``tests/api/test_features_capabilities.py``.
    """
    _pin_install(monkeypatch, "kubernetes")

    cpu = _environment()["cpu"]

    assert cpu["model"] == "QEMU Virtual CPU version 2.5+"
    assert cpu["hypervisor"] is True
    assert cpu["flags_known"] is True
    assert cpu["x86_64_level"] == "v1"
    assert cpu["missing"][:4] == ["ssse3", "sse4_1", "sse4_2", "popcnt"]
    # Same block the runners read directly, not a second parse of the same file.
    assert cpu == runtime_env.cpu_features()


def test_the_handler_cannot_poison_the_cached_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nested blocks are copies, so a caller may edit what it is handed.

    This endpoint stamps two fields onto the description it receives; the VNC
    card's opener will edit the nested ``vnc`` block the same way. A shallow
    copy would hand every later request - and the agent's prompt line - the
    dict this one mutated.
    """
    _pin_install(monkeypatch, "kubernetes")
    _chart_identity_env(monkeypatch)
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "desktop")

    first = _environment()
    first["kubernetes"]["namespace"] = "tampered"
    first["vnc"]["port_forward_commands"].append({"command": "rm -rf /"})

    second = _environment()

    assert second["kubernetes"]["namespace"] == "lee-cremind"
    assert all(
        entry["command"].startswith("kubectl ")
        for entry in second["vnc"]["port_forward_commands"]
    )
