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
                {"resource": "gmail", "scopes": ["openid", "email", "gmail.readonly"]},
                {"resource": "calendar", "scopes": [
                    "openid", "email", "https://www.googleapis.com/auth/calendar",
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
        "openid", "email", "https://www.googleapis.com/auth/calendar",
    ]


def test_google_client_uses_creds_endpoint(monkeypatch):
    _wire(monkeypatch)
    client = gd.google_client()
    assert client["client_id"] == "creds-client.apps.googleusercontent.com"
    assert client["client_secret"] == "secret-xyz"
    assert "https://www.googleapis.com/auth/calendar" in client["scopes"]


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
