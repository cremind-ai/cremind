"""Tests for /api/services/capabilities — focused on the ``ui_features``
contract added so the Electron app can gate tray / jumplist / dock
entries on whether the backend's bundled SPA actually has each route.

The Electron-side tray builder (``ui/electron/main.ts``) reads
``ui_features`` from this response and only surfaces an entry whose
name is in the list. When the field is missing the gate HIDES the
entries — pre-protocol backends predate the SPA routes too, so showing
a menu item the SPA can't service is the regression we're guarding
against (the cross-version-install case v0.1.9-test9's ``--version``
flag enables).

The last section reaches over to ``/api/system/environment``: the tray
descriptor and that endpoint answer the *same* questions about the *same*
process, for the two commands (``cremind server capabilities`` and ``server
environment``) a user runs interchangeably. Whether they agree is a property of
the pair, so it is tested here rather than on either side alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import features as features_api
from app.config import runtime_env


@pytest.fixture(autouse=True)
def _uncached_runtime_env(monkeypatch: pytest.MonkeyPatch):
    """The tray descriptor answers from the shared runtime description.

    That description is lru_cached for the life of the process (it feeds the
    agent's prompt-cached system prompt), so without a clear around every case
    the first install described here would leak into all the others. The
    container marker is pointed at a path that cannot exist because CI itself
    may run inside a container, where ``/.dockerenv`` would turn every native
    row here into a Docker one. The TLS path resolves the same question through
    its own copy of the marker plus the pod signals, so those are neutralised
    too — the ``local_trust`` and ``restart_supported`` rows read that one.
    """
    monkeypatch.setattr(runtime_env, "_CONTAINER_MARKER", Path("/nonexistent/.dockerenv"))
    monkeypatch.setattr(
        "app.config.tls_managed_env._CONTAINER_MARKER", Path("/nonexistent/.dockerenv"))
    monkeypatch.setattr(
        "app.config.tls_managed_env._POD_MARKER", Path("/nonexistent/serviceaccount"))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    runtime_env.describe_runtime_environment.cache_clear()
    yield
    runtime_env.describe_runtime_environment.cache_clear()


def _make_request(client_host: str | None = None) -> object:
    """Minimal Starlette-compatible Request stand-in.

    ``get_service_capabilities`` only calls ``require_admin(request)`` on
    setup-complete installs; we route the test through the
    setup-not-complete branch (the pre-auth wizard path) so the only
    request attribute read is ``client`` (by the ``tls.local_trust``
    block, which checks whether the peer is loopback).
    """
    client = SimpleNamespace(host=client_host, port=12345) if client_host else None
    return SimpleNamespace(headers={}, cookies={}, client=client)


def _stub_state(monkeypatch: pytest.MonkeyPatch, *, setup_complete: bool = False) -> None:
    """Stub :func:`app.runtime.get_state` so the endpoint runs without
    a real storage layer. ``setup_complete=False`` keeps the endpoint
    in its unauthenticated branch — that's the pre-token wizard path
    the Electron app exercises before the user has signed in."""
    fake_state = SimpleNamespace(
        storage_ready=setup_complete,
        config_storage=SimpleNamespace(
            is_setup_complete=lambda: setup_complete,
        ),
    )
    monkeypatch.setattr(features_api, "get_state", lambda: fake_state)


# Everything the shared description reads that a dev box (or a CI runner
# inside a container) may already have set. The VNC half was added when the
# descriptor moved into that description: a stray ``CREMIND_NOVNC_URL`` or
# ``CREMIND_SSL`` from a real install flips the access shape a case here is
# pinning, and the four ``CREMIND_K8S_`` keys plus ``KUBERNETES_SERVICE_HOST``
# / ``HOSTNAME`` would let a real cluster name itself inside these rows.
_SCRUBBED_ENV = (
    "CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED", "VNC_PASSWORD",
    "CREMIND_NOVNC_URL", "NOVNC_PORT", "CREMIND_SSL",
    "CREMIND_TLS_TERMINATION", "CREMIND_COMPOSE_ENV_FILE", "APP_URL",
    "CREMIND_K8S_NAMESPACE", "CREMIND_K8S_RELEASE", "CREMIND_K8S_WORKLOAD",
    "CREMIND_K8S_SERVICE_PORT", "KUBERNETES_SERVICE_HOST", "HOSTNAME",
)


def _install(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Pin the install the tray descriptor should describe.

    It reads the shared runtime description, which resolves the mode from
    ``INSTALL_MODE`` itself — patching ``features_api.get_active_install_mode``
    (still the source for the wizard's own endpoint) would not reach it.
    """
    monkeypatch.setenv("INSTALL_MODE", mode)
    for name in _SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)


def test_ui_features_list_matches_electron_tray_entries() -> None:
    """The Electron tray / jumplist / dock builders call
    ``uiFeatureAvailable('processes' | 'events' | 'channels')`` — those
    names must be exactly the ones the backend ships. Drift on either
    side breaks the gate."""
    assert "processes" in features_api.UI_FEATURES
    assert "events" in features_api.UI_FEATURES
    assert "channels" in features_api.UI_FEATURES


def test_capabilities_response_includes_ui_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capabilities endpoint must include ``ui_features`` in its
    JSON response so Electron's ``fetchCapabilities`` can populate the
    gate. Without the field, Electron's fallback is to HIDE every gated
    entry — so a missing field here would make the menu items vanish
    against an otherwise-up-to-date backend."""
    _stub_state(monkeypatch)
    # The function doesn't need a real services payload or install
    # catalog for this assertion — stub them out so the test stays a
    # focused contract check on the response shape.
    monkeypatch.setattr(features_api, "docker_available", lambda: False)
    monkeypatch.setattr(features_api, "get_capabilities_payload", lambda: {})
    monkeypatch.setattr(features_api, "get_active_install_mode", lambda: None)
    monkeypatch.setattr(features_api, "apply_mode_rule_to_services", lambda _p, _m: None)

    response = asyncio.run(features_api.get_service_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert "ui_features" in body, (
        "Missing ui_features in /api/services/capabilities; Electron "
        "would hide every gated tray entry."
    )
    assert sorted(body["ui_features"]) == sorted(features_api.UI_FEATURES)


def test_capabilities_response_preserves_existing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-suspenders: adding ``ui_features`` must not displace
    the fields the Electron app and Setup Wizard already depend on
    (``install_mode``, ``docker_available``, ``services``)."""
    _stub_state(monkeypatch)
    monkeypatch.setattr(features_api, "docker_available", lambda: True)
    monkeypatch.setattr(features_api, "get_capabilities_payload", lambda: {"x": 1})
    monkeypatch.setattr(features_api, "get_active_install_mode", lambda: "docker")
    monkeypatch.setattr(features_api, "apply_mode_rule_to_services", lambda _p, _m: None)

    response = asyncio.run(features_api.get_service_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert body["install_mode"] == "docker"
    assert body["docker_available"] is True
    assert body["services"] == {"x": 1}


def test_tray_capabilities_returns_features_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Electron main process can't share the renderer's session
    cookies, so the tray-gating endpoint must stay reachable after setup
    completes. Set ``setup_complete=True`` to prove the new endpoint
    skips the admin gate that breaks ``/api/services/capabilities``."""
    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "native")

    response = asyncio.run(features_api.get_tray_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["install_mode"] == "native"
    assert sorted(body["ui_features"]) == sorted(features_api.UI_FEATURES)
    assert body["supervised"] is False


def test_tray_capabilities_reports_a_boot_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``install_mode`` alone cannot say whether a native install is supervised.

    Both the Developer page's restart warning and ``cremind server restart``
    read this field; without it they tell a user with a boot service that
    their backend will stay down.
    """
    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "native")
    monkeypatch.setenv("CREMIND_SUPERVISED", "1")

    response = asyncio.run(features_api.get_tray_capabilities(_make_request()))
    import json

    assert json.loads(response.body)["supervised"] is True


def test_tray_capabilities_reports_a_container_as_supervised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same question, same answer as ``/api/system/environment``.

    Nothing in install/ or helm/ sets ``CREMIND_SUPERVISED`` for a container —
    compose sets ``restart: unless-stopped`` and the kubelet restarts the pod
    instead — so reading only that variable here told a Docker user their
    backend would stay down while the environment endpoint said the opposite
    about the very same process.
    """
    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "docker")
    monkeypatch.delenv("CREMIND_SUPERVISED", raising=False)

    response = asyncio.run(features_api.get_tray_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert body["supervised"] is True
    assert body["supervised"] is runtime_env.describe_runtime_environment()["supervised"]


def test_tray_install_mode_matches_the_environment_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-flavor Docker image writes no INSTALL_MODE, only VNC_PASSWORD.

    ``get_active_install_mode()`` returns None there, so the tray descriptor
    used to say "unknown install" while ``server environment`` said "docker"
    about the same container. Both now read the one shared description — which
    also lets the Electron client offer "Open VNC Desktop" on those images,
    every one of which has the desktop.
    """
    _stub_state(monkeypatch, setup_complete=True)
    monkeypatch.delenv("INSTALL_MODE", raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.setenv("VNC_PASSWORD", "secret")

    response = asyncio.run(features_api.get_tray_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert body["install_mode"] == "docker"
    assert body["vnc_enabled"] is True


# ── image_flavor gate (drives Electron's "Open VNC Desktop" entry) ─────────


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("desktop", "desktop"),
        ("basic", "basic"),
        ("DESKTOP", "desktop"),   # case-insensitive
        ("  basic  ", "basic"),   # trimmed
        ("garbage", None),        # unknown → None
        ("", None),               # empty → None
    ],
)
def test_get_image_flavor_normalization(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: str | None,
) -> None:
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", env_value)
    assert features_api.get_image_flavor() == expected


def test_get_image_flavor_unset_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native installs and pre-flavor images have no env var → None (the
    Electron client treats None as desktop for Docker installs)."""
    monkeypatch.delenv("CREMIND_IMAGE_FLAVOR", raising=False)
    assert features_api.get_image_flavor() is None


def test_tray_capabilities_includes_image_flavor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tray descriptor must carry ``image_flavor`` so Electron can hide
    "Open VNC Desktop" on the basic image."""
    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "docker")
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "basic")

    response = asyncio.run(features_api.get_tray_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert body["image_flavor"] == "basic"


def test_service_capabilities_includes_image_flavor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_state(monkeypatch)
    monkeypatch.setattr(features_api, "docker_available", lambda: True)
    monkeypatch.setattr(features_api, "get_capabilities_payload", lambda: {})
    monkeypatch.setattr(features_api, "get_active_install_mode", lambda: "docker")
    monkeypatch.setattr(features_api, "apply_mode_rule_to_services", lambda _p, _m: None)
    monkeypatch.delenv("CREMIND_IMAGE_FLAVOR", raising=False)

    response = asyncio.run(features_api.get_service_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert body["image_flavor"] is None


# ── the ``vnc`` descriptor (drives "Open VNC Desktop") ────────────────────
#
# This endpoint answers with no token at all, so what it may say about the
# desktop is exactly four fields: is there one, how is it reached, on what
# path, on what port. Namespaces, Service names, ready-to-run kubectl lines
# and the composed URL are cluster topology and stay admin-only.

_PUBLIC_VNC_KEYS = {"enabled", "access", "novnc_path", "novnc_port"}


def _tray() -> dict:
    import json

    return json.loads(
        asyncio.run(features_api.get_tray_capabilities(_make_request())).body
    )


def test_tray_capabilities_publishes_the_public_vnc_subset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Docker: enough to open the desktop, and nothing else.

    The Electron shell used to gate its menu entry on ``install_mode ==
    'docker' && image_flavor != 'basic'`` and then guess ``localhost:6080``.
    It reads ``vnc.access`` now, so the four fields below are load-bearing.
    """
    from app.config.settings import BaseConfig

    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "docker")
    # A pre-flavor image sets neither, and both make the desktop go away.
    monkeypatch.delenv("CREMIND_IMAGE_FLAVOR", raising=False)
    # ``_novnc_port`` walks the installer's docker/.env when NOVNC_PORT is
    # unset; a dev box with a real install would otherwise answer from it.
    monkeypatch.setattr(
        BaseConfig, "CREMIND_INSTALL_DIR", str(tmp_path), raising=False,
    )

    vnc = _tray()["vnc"]

    assert set(vnc) == _PUBLIC_VNC_KEYS
    assert vnc == {
        "enabled": True,
        "access": "direct",
        "novnc_path": "/vnc.html",
        "novnc_port": 6080,
    }


def test_tray_capabilities_hides_the_kubernetes_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay shape is the one with something worth hiding.

    With in-pod TLS the sidecar stops proxying noVNC, so the full descriptor
    carries two ``kubectl port-forward`` lines naming the namespace and the
    Service. An unauthenticated caller gets the shape and the port — enough to
    know a tunnel is needed — and none of the names.
    """
    import json

    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "kubernetes")
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "desktop")
    monkeypatch.setenv("CREMIND_NOVNC_URL", "http://localhost:6080/vnc.html")
    monkeypatch.setenv("CREMIND_K8S_NAMESPACE", "lee-cremind")
    monkeypatch.setenv("CREMIND_K8S_RELEASE", "cremind")
    monkeypatch.setenv("CREMIND_K8S_WORKLOAD", "cremind")

    body = _tray()
    vnc = body["vnc"]

    assert set(vnc) == _PUBLIC_VNC_KEYS
    assert vnc == {
        "enabled": True,
        "access": "port_forward",
        "novnc_path": "/vnc.html",
        "novnc_port": 6080,
    }
    # Not merely absent from ``vnc``: absent from the whole response.
    assert "kubernetes" not in body
    assert "lee-cremind" not in json.dumps(body)


# ── the ``tls`` block ────────────────────────────────────────────────────
#
# The Setup Wizard reads this BEFORE any admin token exists, to decide whether
# to offer the "trust the CA" step and where to send the browser afterwards.


def _capabilities(monkeypatch) -> dict:
    _stub_state(monkeypatch, setup_complete=False)
    resp = asyncio.run(features_api.get_service_capabilities(_make_request()))
    import json

    return json.loads(resp.body)


def _tls_env(monkeypatch, tmp_path, mode: str, *, serving: bool) -> None:
    from app.config import tls_mode
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "SSL_MODE", mode, raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_CERTFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE", "", raising=False)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    monkeypatch.delenv("CREMIND_ELECTRON_PARENT", raising=False)
    monkeypatch.delenv("CREMIND_UI_PORT", raising=False)
    # Container signals outrank a host ``INSTALL_MODE`` now, and the desktop
    # image bakes ``VNC_PASSWORD``: leaving these in place would make the
    # native cases below Docker ones wherever this suite happens to run.
    monkeypatch.delenv("VNC_PASSWORD", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("INSTALL_MODE", "kubernetes")
    tls_mode.record_boot_tls(serving)


def test_tls_block_is_present_with_every_field(monkeypatch, tmp_path) -> None:
    _tls_env(monkeypatch, tmp_path, "", serving=False)
    tls = _capabilities(monkeypatch)["tls"]
    assert set(tls) == {
        "mode", "serving_https", "pending_https",
        "ca_sha256", "https_url", "restart_supported", "local_trust",
    }
    assert set(tls["local_trust"]) == {
        "supported", "store", "os_prompt", "already_trusted", "reason",
    }


def test_after_setup_reports_pending_with_the_https_url(monkeypatch, tmp_path) -> None:
    """What makes the wizard show the step and know where it is going."""
    _tls_env(monkeypatch, tmp_path, "after-setup", serving=False)

    tls = _capabilities(monkeypatch)["tls"]

    assert tls["mode"] == "after-setup"
    assert tls["serving_https"] is False
    assert tls["pending_https"] is True
    # APP_URL is http here; the wizard must still be sent to https.
    assert tls["https_url"] == "https://localhost:1515"
    assert tls["restart_supported"] is True


def test_the_ca_fingerprint_is_published_once_generated(monkeypatch, tmp_path) -> None:
    from app.config.tls_auto import ensure_local_tls

    _tls_env(monkeypatch, tmp_path, "after-setup", serving=False)
    assert _capabilities(monkeypatch)["tls"]["ca_sha256"] is None

    ensure_local_tls(str(tmp_path))
    fingerprint = _capabilities(monkeypatch)["tls"]["ca_sha256"]

    assert fingerprint and fingerprint.count(":") == 31, fingerprint
    assert fingerprint == fingerprint.upper()


def test_electron_can_prepare_https(monkeypatch, tmp_path) -> None:
    """Desktop windows now support the same HTTPS transition."""
    _tls_env(monkeypatch, tmp_path, "after-setup", serving=False)
    monkeypatch.setenv("CREMIND_ELECTRON_PARENT", "1")

    tls = _capabilities(monkeypatch)["tls"]

    assert tls["pending_https"] is True
    assert tls["https_url"] == "https://localhost:1515"


def test_plain_http_reports_nothing_pending(monkeypatch, tmp_path) -> None:
    _tls_env(monkeypatch, tmp_path, "", serving=False)
    tls = _capabilities(monkeypatch)["tls"]
    assert tls["pending_https"] is False and tls["https_url"] is None


def test_a_native_install_cannot_restart_itself(monkeypatch, tmp_path) -> None:
    _tls_env(monkeypatch, tmp_path, "after-setup", serving=False)
    monkeypatch.setenv("INSTALL_MODE", "native")
    assert _capabilities(monkeypatch)["tls"]["restart_supported"] is False


# ── the ``tls.local_trust`` block (one-click trust) ───────────────────────
#
# ``supported`` must be true ONLY when POSTing /api/tls/trust would land the
# CA in the right device's store: a native install answering its own
# machine's browser. Everything else degrades to the manual commands.


def _local_trust(
    monkeypatch, tmp_path, *, install_mode: str, client_host: str,
    generate_ca: bool = False,
) -> dict:
    import app.api.tls as tls_api
    from app.config.tls_trust import TrustPlan

    _tls_env(monkeypatch, tmp_path, "after-setup", serving=False)
    monkeypatch.setenv("INSTALL_MODE", install_mode)
    if generate_ca:
        from app.config.tls_auto import ensure_local_tls

        ensure_local_tls(str(tmp_path))
    # Platform-independent: the per-OS plan logic has its own unit tests
    # (tests/config/test_tls_trust.py); here only the wiring is under test.
    monkeypatch.setattr(
        tls_api,
        "server_trust_plan",
        lambda _p: TrustPlan(
            supported=True,
            store="test store",
            commands=[["certutil", "-addstore", "-user", "Root", "x"]],
            os_prompt="windows",
        ),
    )
    monkeypatch.setattr(tls_api, "already_trusted", lambda _p: False)
    _stub_state(monkeypatch, setup_complete=False)
    resp = asyncio.run(
        features_api.get_service_capabilities(_make_request(client_host))
    )
    import json

    return json.loads(resp.body)["tls"]["local_trust"]


def test_local_trust_offered_to_a_native_installs_own_browser(
    monkeypatch, tmp_path,
) -> None:
    lt = _local_trust(
        monkeypatch, tmp_path,
        install_mode="native", client_host="127.0.0.1", generate_ca=True,
    )
    assert lt["supported"] is True
    assert lt["store"] == "test store"
    assert lt["os_prompt"] == "windows"
    assert lt["already_trusted"] is False


def test_local_trust_refused_for_containers(monkeypatch, tmp_path) -> None:
    """A container can only write its own store — never the browser's."""
    for mode in ("docker", "kubernetes"):
        lt = _local_trust(
            monkeypatch, tmp_path, install_mode=mode, client_host="127.0.0.1",
        )
        assert lt["supported"] is False, mode
        assert lt["reason"], mode


def test_local_trust_refused_for_a_remote_browser(monkeypatch, tmp_path) -> None:
    """A LAN client of a native install must get the manual commands —
    trusting server-side would land the CA on the wrong device."""
    lt = _local_trust(
        monkeypatch, tmp_path, install_mode="native", client_host="192.168.1.20",
    )
    assert lt["supported"] is False


def test_local_trust_refused_without_a_ca(monkeypatch, tmp_path) -> None:
    """No CA on disk (plain HTTP, or an operator certificate pair) — there
    is nothing of ours to trust, so no button."""
    lt = _local_trust(
        monkeypatch, tmp_path, install_mode="native", client_host="127.0.0.1",
    )
    assert lt["supported"] is False


# ── agreement with /api/system/environment ───────────────────────────────


def _admin_environment(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The admin-gated environment body, as ``cremind server environment`` sees it."""
    from app.api import system as system_api

    request = SimpleNamespace(
        headers={}, cookies={}, client=None,
        user=SimpleNamespace(is_authenticated=True, username="admin"),
    )
    response = asyncio.run(system_api.get_system_environment(request))
    import json

    return json.loads(response.body)


def test_both_endpoints_describe_one_container_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Docker install where ``server capabilities`` said "supervised: false"
    and ``server environment`` said "supervised: true" was describing one
    process two ways — the same trap for ``install_mode``."""
    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "docker")

    import json

    tray = json.loads(
        asyncio.run(features_api.get_tray_capabilities(_make_request())).body
    )
    environment = _admin_environment(monkeypatch)

    assert tray["supervised"] is environment["supervised"] is True
    assert tray["install_mode"] == environment["install_mode"] == "docker"


def test_tray_vnc_access_matches_the_environment_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One desktop, one answer about how it is reached.

    The tray descriptor decides whether the Electron menu entry exists; the
    admin endpoint decides what the Developer page's card renders. They read
    the same block, and the only difference must be how much of it travels —
    the commands and the URL stay behind the admin gate.
    """
    import json

    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "kubernetes")
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "desktop")
    monkeypatch.setenv("CREMIND_NOVNC_URL", "http://localhost:6080/vnc.html")
    monkeypatch.setenv("CREMIND_K8S_NAMESPACE", "lee-cremind")
    monkeypatch.setenv("CREMIND_K8S_RELEASE", "cremind")
    monkeypatch.setenv("CREMIND_K8S_WORKLOAD", "cremind")

    tray = json.loads(
        asyncio.run(features_api.get_tray_capabilities(_make_request())).body
    )["vnc"]
    environment = _admin_environment(monkeypatch)["vnc"]

    assert tray["access"] == environment["access"] == "port_forward"
    assert tray["enabled"] is environment["enabled"] is True
    assert tray["novnc_port"] == environment["novnc_port"] == 6080
    # What the admin endpoint adds on top, and the tray must not.
    assert environment["port_forward_commands"], "the relay shape needs a tunnel"
    assert environment["novnc_url"] == "http://localhost:6080/vnc.html"
    assert "port_forward_commands" not in tray and "novnc_url" not in tray


def test_the_tray_never_carries_a_cpu_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CPU facts are admin-only, and this endpoint answers with no token.

    ``/api/system/environment`` publishes the CPU model and the instruction-set
    flags it lacks, because that is the only thing that explains a coding-agent
    CLI hanging on a node whose hypervisor hides them. None of it belongs here:
    a model name plus a flag list is a precise fingerprint of the machine and of
    the hypervisor underneath it, handed to anyone who can reach the port. This
    endpoint picks its fields explicitly rather than spreading the description,
    which is what keeps that true - so the assertion is on the whole body, not
    just on the key.
    """
    import json

    _stub_state(monkeypatch, setup_complete=True)
    _install(monkeypatch, "kubernetes")

    body = _tray()

    assert "cpu" not in body
    assert "x86_64" not in json.dumps(body)


def test_environment_reports_the_zone_schedules_actually_fire_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CREMIND_TIMEZONE`` is blank on a normal install, and the timezone the
    admin set on the Config page lives in ``user_config`` — so reporting the
    env var alone rendered "system default" on the Environment card while the
    scheduler was firing in Asia/Tokyo.
    """
    from app.config import timezone as timezone_config

    _install(monkeypatch, "native")
    monkeypatch.delenv("CREMIND_TIMEZONE", raising=False)
    monkeypatch.setattr(
        timezone_config, "get_dynamic",
        lambda _t, _k, profile=None: "Asia/Tokyo" if profile == "admin" else None,
    )

    body = _admin_environment(monkeypatch)

    assert body["effective_timezone"] == "Asia/Tokyo"
    assert body["boot_timezone"] == ""
    # The pre-split field answered a different question under a name that
    # claimed this one; leaving it would keep every consumer guessing.
    assert "timezone" not in body
