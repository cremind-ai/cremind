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


def _make_request() -> object:
    """Minimal Starlette-compatible Request stand-in.

    ``get_service_capabilities`` only calls ``require_admin(request)`` on
    setup-complete installs; we route the test through the
    setup-not-complete branch (the pre-auth wizard path) so we never
    touch the request object's interface.
    """
    return SimpleNamespace(headers={}, cookies={})


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
