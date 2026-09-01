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
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api import features as features_api


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
    monkeypatch.setattr(features_api, "get_active_install_mode", lambda: "native")

    response = asyncio.run(features_api.get_tray_capabilities(_make_request()))
    import json

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["install_mode"] == "native"
    assert sorted(body["ui_features"]) == sorted(features_api.UI_FEATURES)


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
    monkeypatch.setattr(features_api, "get_active_install_mode", lambda: "docker")
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


def test_electron_is_never_pending(monkeypatch, tmp_path) -> None:
    """The server refuses TLS under Electron, so the wizard must not pivot."""
    _tls_env(monkeypatch, tmp_path, "after-setup", serving=False)
    monkeypatch.setenv("CREMIND_ELECTRON_PARENT", "1")

    tls = _capabilities(monkeypatch)["tls"]

    assert tls["pending_https"] is False
    assert tls["https_url"] is None


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
