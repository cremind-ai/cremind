"""Tests for cremind-connect discovery parsing (Google Calendar connect)."""

from __future__ import annotations

import app.calendar.google_discovery as gd

_WELL_KNOWN = {
    "relay": {"wsUrl": "wss://connect.example/subscribe"},
    "providers": [
        {
            "provider": "google",
            "authClientId": "doc-client.apps.googleusercontent.com",
            "scopes": ["openid", "email"],
            "resources": [
                {"resource": "gmail", "scopes": [
                    "openid", "email", "https://www.googleapis.com/auth/gmail.send",
                ]},
                {"resource": "calendar", "scopes": [
                    "openid", "email", "https://www.googleapis.com/auth/calendar.events",
                ]},
            ],
        }
    ],
}
_CREDS = {"clientId": "creds-client.apps.googleusercontent.com", "clientSecret": "secret-xyz"}


def _wire(monkeypatch, *, well_known=_WELL_KNOWN, creds=_CREDS):
    gd.reset_cache()
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    def fake_get_json(url, timeout=15.0):
        if url.endswith("/.well-known/cremind-connect"):
            return well_known
        if url.endswith("/credentials/google"):
            return creds
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(gd, "_get_json", fake_get_json)


def test_calendar_scopes_picks_resource(monkeypatch):
    _wire(monkeypatch)
    assert gd.calendar_scopes() == [
        "openid", "email", "https://www.googleapis.com/auth/calendar.events",
    ]


def test_google_client_uses_creds_endpoint(monkeypatch):
    _wire(monkeypatch)
    client = gd.google_client()
    assert client["client_id"] == "creds-client.apps.googleusercontent.com"
    assert client["client_secret"] == "secret-xyz"
    assert "https://www.googleapis.com/auth/calendar.events" in client["scopes"]


def test_env_overrides_win(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "env-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "env-secret")
    client = gd.google_client()
    assert client["client_id"] == "env-client"
    assert client["client_secret"] == "env-secret"


def test_scopes_fallback_when_no_calendar_resource(monkeypatch):
    wk = {"providers": [{"provider": "google", "authClientId": "x", "resources": []}]}
    _wire(monkeypatch, well_known=wk)
    assert gd.calendar_scopes() == gd.CALENDAR_SCOPES_FALLBACK


# --- resource_scopes_or_none: "we could not ask" must not read as an answer ---


def _wire_outage(monkeypatch):
    gd.reset_cache()

    def _boom(url, timeout=15.0):
        raise OSError("connect refused")

    monkeypatch.setattr(gd, "_get_json", _boom)


def test_resource_scopes_or_none_returns_the_advertised_set(monkeypatch):
    _wire(monkeypatch)
    assert gd.resource_scopes_or_none("calendar") == [
        "openid", "email", "https://www.googleapis.com/auth/calendar.events",
    ]


def test_resource_scopes_or_none_is_none_on_outage(monkeypatch):
    _wire_outage(monkeypatch)
    assert gd.resource_scopes_or_none("calendar") is None
    assert gd.resource_scopes_or_none("drive") is None


def test_resource_scopes_or_none_is_none_when_nothing_is_advertised(monkeypatch):
    wk = {"providers": [{"provider": "google", "authClientId": "x", "resources": []}]}
    _wire(monkeypatch, well_known=wk)
    assert gd.resource_scopes_or_none("calendar") is None


def test_linking_still_falls_back_during_an_outage(monkeypatch):
    """resource_scopes keeps guessing on purpose — a link must still be possible."""
    _wire_outage(monkeypatch)
    assert gd.calendar_scopes() == gd.CALENDAR_SCOPES_FALLBACK
    assert gd.resource_scopes("drive", ["a", "b"]) == ["a", "b"]
