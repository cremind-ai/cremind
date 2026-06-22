"""Tests for backend-native Google OAuth: authorize URL, callback exchange,
per-profile token storage, and refresh — all with httpx + storage mocked."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

import app.calendar.google_auth as ga


class FakeStorage:
    def __init__(self):
        self.d: dict[tuple, str] = {}

    def save_token(self, agent_name, profile, token, agent_type="a2a", token_kind="access_token"):
        self.d[(agent_name, profile, agent_type, token_kind)] = token

    def get_token(self, agent_name, profile, agent_type="a2a", token_kind="access_token"):
        return self.d.get((agent_name, profile, agent_type, token_kind), "")

    def delete_token(self, agent_name, profile, agent_type="a2a", token_kind="access_token"):
        return self.d.pop((agent_name, profile, agent_type, token_kind), None) is not None


def _id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


def _wire(monkeypatch):
    store = FakeStorage()
    monkeypatch.setattr(ga, "get_auth_client_storage", lambda: store)
    monkeypatch.setattr(
        ga.google_discovery, "google_client",
        lambda: {"client_id": "cid", "client_secret": "csecret", "scopes": ["openid", "email", "cal"]},
    )
    monkeypatch.setattr(ga.BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    ga._pending.clear()
    return store


def test_build_authorize_url_and_pending(monkeypatch):
    _wire(monkeypatch)
    url = ga.build_authorize_url("alice")
    assert url and url.startswith(ga.AUTH_ENDPOINT)
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"][0].endswith(ga.CALLBACK_PATH)
    state = q["state"][0]
    assert state in ga._pending
    assert ga._pending[state]["profile"] == "alice"
    # Least-privilege: ONLY calendar.events, and no incremental-auth re-request.
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert "include_granted_scopes" not in q


def test_complete_callback_stores_tokens(monkeypatch):
    store = _wire(monkeypatch)
    monkeypatch.setattr(ga, "_post_token", lambda data: {
        "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
        "id_token": _id_token("alice@example.com"),
    })
    url = ga.build_authorize_url("alice")
    state = parse_qs(urlparse(url).query)["state"][0]

    result = ga.complete_callback(state, "the-code")
    assert result == {"profile": "alice", "email": "alice@example.com"}
    assert store.get_token(ga.AGENT_NAME, "alice", agent_type=ga.AGENT_TYPE, token_kind=ga.ACCESS_TOKEN) == "AT"
    assert store.get_token(ga.AGENT_NAME, "alice", agent_type=ga.AGENT_TYPE, token_kind=ga.REFRESH_TOKEN) == "RT"
    st = ga.status("alice")
    assert st["connected"] is True and st["email"] == "alice@example.com"
    assert state not in ga._pending


def test_complete_callback_unknown_state(monkeypatch):
    _wire(monkeypatch)
    with pytest.raises(ga.GoogleAuthError):
        ga.complete_callback("nope", "code")


def test_get_access_token_refreshes_when_expired(monkeypatch):
    store = _wire(monkeypatch)
    store.save_token(ga.AGENT_NAME, "bob", "OLD", agent_type=ga.AGENT_TYPE, token_kind=ga.ACCESS_TOKEN)
    store.save_token(ga.AGENT_NAME, "bob", "RT", agent_type=ga.AGENT_TYPE, token_kind=ga.REFRESH_TOKEN)
    store.save_token(
        ga.AGENT_NAME, "bob",
        json.dumps({"email": "bob@x.com", "expiry": time.time() - 10, "scopes": []}),
        agent_type=ga.AGENT_TYPE, token_kind=ga.META_KIND,
    )
    calls = {}

    def fake_post(data):
        calls.update(data)
        return {"access_token": "NEW", "expires_in": 3600}

    monkeypatch.setattr(ga, "_post_token", fake_post)
    tok = ga.get_access_token("bob")
    assert tok == "NEW"
    assert calls["grant_type"] == "refresh_token"
    assert store.get_token(ga.AGENT_NAME, "bob", agent_type=ga.AGENT_TYPE, token_kind=ga.ACCESS_TOKEN) == "NEW"


def test_get_access_token_valid_not_refreshed(monkeypatch):
    store = _wire(monkeypatch)
    store.save_token(ga.AGENT_NAME, "bob", "CUR", agent_type=ga.AGENT_TYPE, token_kind=ga.ACCESS_TOKEN)
    store.save_token(
        ga.AGENT_NAME, "bob",
        json.dumps({"email": "bob@x.com", "expiry": time.time() + 3600, "scopes": []}),
        agent_type=ga.AGENT_TYPE, token_kind=ga.META_KIND,
    )

    def boom(_data):
        raise AssertionError("should not refresh a valid token")

    monkeypatch.setattr(ga, "_post_token", boom)
    assert ga.get_access_token("bob") == "CUR"


def test_disconnect_clears_tokens(monkeypatch):
    store = _wire(monkeypatch)
    for kind in (ga.ACCESS_TOKEN, ga.REFRESH_TOKEN, ga.META_KIND):
        store.save_token(ga.AGENT_NAME, "carol", "v", agent_type=ga.AGENT_TYPE, token_kind=kind)
    ga.disconnect("carol")
    assert ga.status("carol")["connected"] is False
