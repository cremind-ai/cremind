"""Tests for backend-native Google OAuth: authorize URL, callback exchange,
per-profile token storage, refresh, and which of the two possible Google
credentials wins — all with httpx + storage mocked."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

import app.calendar.google_auth as ga
from app.config.settings import BaseConfig


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


_NO_SKILL = {
    "linked": False, "enabled": False, "effective": False,
    "email": None, "scopes": [], "account_key": None,
}


def _skill(monkeypatch, *, effective: bool, email: str | None = None, token: str | None = None):
    """Pretend the gcalendar skill is (or is not) linked, without a filesystem."""
    monkeypatch.setattr(
        ga.skill_token, "status",
        lambda profile: {**_NO_SKILL, "linked": effective, "enabled": effective,
                         "effective": effective, "email": email},
    )
    monkeypatch.setattr(ga.skill_token, "is_effective", lambda profile: effective)
    monkeypatch.setattr(ga.skill_token, "access_token", lambda profile: token)


def _wire(monkeypatch):
    store = FakeStorage()
    monkeypatch.setattr(ga, "get_auth_client_storage", lambda: store)
    monkeypatch.setattr(
        ga.google_discovery, "google_client",
        lambda: {"client_id": "cid", "client_secret": "csecret", "scopes": ["openid", "email", "cal"]},
    )
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    # The redirect also reads these; a developer shell exporting one would skew
    # every redirect assertion below.
    for key in ("CREMIND_OAUTH_REDIRECT_URI", "CREMIND_UI_PORT", "CREMIND_TLS_TERMINATION"):
        monkeypatch.delenv(key, raising=False)
    # No gcalendar skill link unless a test says so — and never a real one read off
    # the developer's own machine.
    _skill(monkeypatch, effective=False)
    ga._pending.clear()
    return store


def _advertised(url: str) -> str:
    return parse_qs(urlparse(url).query)["redirect_uri"][0]


# ── the redirect Google is handed ───────────────────────────────────────────

def test_redirect_is_the_http_loopback_app_url(monkeypatch):
    _wire(monkeypatch)
    assert ga.redirect_uri() == "http://localhost:1515" + ga.CALLBACK_PATH
    assert _advertised(ga.build_authorize_url("alice")) == "http://localhost:1515" + ga.CALLBACK_PATH


def test_https_loopback_app_url_advertises_http_on_the_same_port(monkeypatch):
    """Google refuses an https loopback redirect for the Desktop client; the
    same-port TLS listener bounces the plaintext callback to the HTTPS handler."""
    _wire(monkeypatch)
    for app_url, expected in (
        ("https://localhost:1515", "http://localhost:1515"),
        ("https://127.0.0.1:8443/", "http://127.0.0.1:8443"),
        ("https://[::1]:1515", "http://[::1]:1515"),
    ):
        monkeypatch.setattr(BaseConfig, "APP_URL", app_url, raising=False)
        assert ga.redirect_uri() == expected + ga.CALLBACK_PATH, app_url
        assert _advertised(ga.build_authorize_url("alice")) == expected + ga.CALLBACK_PATH


def test_a_public_app_url_is_never_handed_to_google(monkeypatch):
    """The shared Desktop client rejects a public hostname with a 400 before
    consent, which is what advertising the raw APP_URL here used to do."""
    _wire(monkeypatch)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com", raising=False)
    url = ga.build_authorize_url("alice")
    assert url is not None  # no longer "unavailable" on an Ingress install
    assert _advertised(url) == "http://localhost:1515" + ga.CALLBACK_PATH

    monkeypatch.setenv("CREMIND_UI_PORT", "8080")
    assert ga.redirect_uri() == "http://localhost:8080" + ga.CALLBACK_PATH

    # The operator's loopback pin names the forwarded address.
    monkeypatch.setenv("CREMIND_OAUTH_REDIRECT_URI", "http://127.0.0.1:9999/api/oauth/callback")
    assert ga.redirect_uri() == "http://127.0.0.1:9999" + ga.CALLBACK_PATH

    # So does a LAN or listen-all APP_URL: loopback on its port, never its host.
    monkeypatch.delenv("CREMIND_OAUTH_REDIRECT_URI")
    monkeypatch.setattr(BaseConfig, "APP_URL", "http://192.168.1.50:1515", raising=False)
    assert ga.redirect_uri() == "http://localhost:1515" + ga.CALLBACK_PATH


def test_the_exchange_posts_the_advertised_redirect_uri(monkeypatch):
    """Google compares the exchange's redirect_uri with the authorize request's.

    On an HTTPS install the callback is finally received over https, after the
    plaintext bounce, and APP_URL may even change before the user approves.
    Neither may leak into the exchange: it must replay the http URI advertised.
    """
    _wire(monkeypatch)
    monkeypatch.setattr(BaseConfig, "APP_URL", "https://localhost:1515", raising=False)
    posted = {}

    def fake_post(data):
        posted.update(data)
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    monkeypatch.setattr(ga, "_post_token", fake_post)
    url = ga.build_authorize_url("alice")
    advertised = _advertised(url)
    assert advertised == "http://localhost:1515" + ga.CALLBACK_PATH

    monkeypatch.setattr(BaseConfig, "APP_URL", "https://cremind.example.com:9443", raising=False)
    ga.complete_callback(parse_qs(urlparse(url).query)["state"][0], "the-code")
    assert posted["redirect_uri"] == advertised
    assert posted["grant_type"] == "authorization_code" and posted["code"] == "the-code"


# ── consent + tokens ────────────────────────────────────────────────────────

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


# ── which credential wins ───────────────────────────────────────────────────

def _connect_app(store, profile="dave", *, meta=None):
    """Put an app-connected credential in place, as complete_callback would."""
    store.save_token(ga.AGENT_NAME, profile, "APP-AT", agent_type=ga.AGENT_TYPE, token_kind=ga.ACCESS_TOKEN)
    store.save_token(ga.AGENT_NAME, profile, "APP-RT", agent_type=ga.AGENT_TYPE, token_kind=ga.REFRESH_TOKEN)
    store.save_token(
        ga.AGENT_NAME, profile,
        json.dumps(meta or {"email": None, "expiry": time.time() + 3600,
                            "scopes": ga.CALENDAR_SCOPES, "connection_id": "c1"}),
        agent_type=ga.AGENT_TYPE, token_kind=ga.META_KIND,
    )


def test_the_skill_link_wins_over_a_credential_connected_on_the_page(monkeypatch):
    """Linking in chat is meant to be enough, even if the page connected earlier."""
    store = _wire(monkeypatch)
    _connect_app(store)
    _skill(monkeypatch, effective=True, email="linked@example.com", token="SKILL-AT")

    st = ga.status("dave")
    assert st == {"connected": True, "email": "linked@example.com", "source": ga.SOURCE_SKILL}
    assert ga.get_access_token("dave") == "SKILL-AT"


def test_disabling_the_skill_falls_back_to_the_page_credential(monkeypatch):
    """The dormant rows are kept precisely so this works."""
    store = _wire(monkeypatch)
    _connect_app(store)
    _skill(monkeypatch, effective=False)

    st = ga.status("dave")
    assert st["connected"] is True and st["source"] == ga.SOURCE_APP
    assert ga.get_access_token("dave") == "APP-AT"


def test_an_unusable_skill_token_never_falls_back_to_the_other_account(monkeypatch):
    """Silently writing this profile's events into a different Google account
    would be worse than degrading to the internal calendar."""
    store = _wire(monkeypatch)
    _connect_app(store)
    _skill(monkeypatch, effective=True, email="linked@example.com", token=None)

    assert ga.status("dave")["source"] == ga.SOURCE_SKILL
    assert ga.get_access_token("dave") is None


def test_a_broken_skill_check_cannot_break_the_page_credential(monkeypatch):
    store = _wire(monkeypatch)
    _connect_app(store)

    def boom(_profile):
        raise RuntimeError("skills storage not ready")

    monkeypatch.setattr(ga.skill_token, "status", boom)
    monkeypatch.setattr(ga.skill_token, "is_effective", boom)
    assert ga.status("dave")["source"] == ga.SOURCE_APP
    assert ga.get_access_token("dave") == "APP-AT"


def test_no_credential_at_all_reports_no_source(monkeypatch):
    _wire(monkeypatch)
    assert ga.status("nobody") == {"connected": False, "email": None, "source": None}
    assert ga.get_access_token("nobody") is None


def test_connect_records_an_identity_for_mirror_tracking(monkeypatch):
    """This flow has no email scope, so a random id per connect is the identity."""
    store = _wire(monkeypatch)
    monkeypatch.setattr(ga, "_post_token", lambda data: {
        "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
    })
    url = ga.build_authorize_url("erin")
    ga.complete_callback(parse_qs(urlparse(url).query)["state"][0], "code")

    meta = json.loads(store.get_token(ga.AGENT_NAME, "erin", agent_type=ga.AGENT_TYPE, token_kind=ga.META_KIND))
    assert meta["connection_id"]
    assert ga.effective_identity("erin") == {"source": ga.SOURCE_APP, "key": meta["connection_id"]}

    # A reconnect is a different identity, which is how an account swap is noticed.
    url2 = ga.build_authorize_url("erin")
    ga.complete_callback(parse_qs(urlparse(url2).query)["state"][0], "code")
    assert ga.effective_identity("erin")["key"] != meta["connection_id"]


def test_the_skill_identity_is_the_linked_address(monkeypatch):
    store = _wire(monkeypatch)
    _connect_app(store, "frank")
    _skill(monkeypatch, effective=True, email="linked@example.com", token="SKILL-AT")
    assert ga.effective_identity("frank") == {"source": ga.SOURCE_SKILL, "key": "linked@example.com"}


def test_the_mirror_target_survives_disconnect(monkeypatch):
    """It describes events still sitting in someone's calendar, so it must."""
    store = _wire(monkeypatch)
    _connect_app(store, "gina")
    ga.write_mirror_target("gina", "app:c1")
    ga.disconnect("gina")
    assert ga.status("gina")["connected"] is False
    assert ga.read_mirror_target("gina") == "app:c1"
