"""Tests for the Codex OAuth protocol + refresh helper (app/lib/llm/codex_auth.py).

Covers the authorize-URL builder (exact params, %20-encoded scope), PKCE/state
shapes, id_token claim extraction, the code-exchange (form) and refresh (JSON)
request shapes, refresh-token rotation, unrecoverable-error classification,
persistence + secret flags, and get_valid_access_token's expiry-lead /
staleness / single-flight / re-auth behavior. httpx is faked; no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Optional

import pytest

from app.lib.llm import codex_auth as ca


class FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, bool]] = {}

    def get(self, table: str, key: str, profile: str = "admin") -> Optional[str]:
        row = self.rows.get((profile, key))
        return row[0] if row else None

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin") -> None:
        self.rows[(profile, key)] = (value, is_secret)


def _make_jwt(payload: dict) -> str:
    def _seg(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{_seg({'alg': 'none'})}.{_seg(payload)}.sig"


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _patch_httpx(monkeypatch, resp: _FakeResp, capture: dict):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, json=None, headers=None):
            capture.update({"url": url, "data": data, "json": json, "headers": headers})
            return resp

    monkeypatch.setattr(ca.httpx, "AsyncClient", _FakeClient)


# ── URL / PKCE / claims ─────────────────────────────────────────────────────

def test_build_authorize_url_exact_params():
    url = ca.build_authorize_url("STATE123", "CHALLENGE456")
    assert url.startswith(ca.CODEX_AUTHORIZE_URL + "?")
    query = url.split("?", 1)[1]
    pairs = dict(p.split("=", 1) for p in query.split("&"))
    assert pairs["response_type"] == "code"
    assert pairs["client_id"] == ca.CODEX_CLIENT_ID
    assert pairs["code_challenge"] == "CHALLENGE456"
    assert pairs["code_challenge_method"] == "S256"
    assert pairs["id_token_add_organizations"] == "true"
    assert pairs["codex_cli_simplified_flow"] == "true"
    assert pairs["originator"] == "codex_cli_rs"
    assert pairs["state"] == "STATE123"
    # scope spaces must be %20, never +
    assert pairs["scope"] == "openid%20profile%20email%20offline_access"
    assert "+" not in query


def test_pkce_and_state_shapes():
    verifier, challenge = ca.generate_pkce()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", verifier)
    expected = base64.urlsafe_b64encode(__import__("hashlib").sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    state = ca.generate_state()
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,128}", state)


def test_extract_id_token_claims_nested_and_fallback():
    nested = _make_jwt({
        "email": "a@b.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1", "chatgpt_plan_type": "plus"},
    })
    c = ca.extract_id_token_claims(nested)
    assert c == {"account_id": "acc-1", "plan_type": "plus", "email": "a@b.com"}

    fallback = _make_jwt({"account_id": "acc-2", "plan_type": "pro", "email": "x@y.com"})
    c2 = ca.extract_id_token_claims(fallback)
    assert c2["account_id"] == "acc-2" and c2["plan_type"] == "pro"

    assert ca.extract_id_token_claims("not-a-jwt") == {}
    assert ca.extract_id_token_claims(None) == {}


# ── token endpoint calls ────────────────────────────────────────────────────

def test_exchange_code_form_body(monkeypatch):
    cap: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(200, {"access_token": "AT", "refresh_token": "RT"}), cap)
    tok = asyncio.run(ca.exchange_code("the-code", "the-verifier"))
    assert tok["access_token"] == "AT"
    assert cap["url"] == ca.CODEX_TOKEN_URL
    # form-encoded (data=), not JSON
    assert cap["json"] is None
    assert cap["data"]["grant_type"] == "authorization_code"
    assert cap["data"]["code"] == "the-code"
    assert cap["data"]["code_verifier"] == "the-verifier"
    assert cap["data"]["redirect_uri"] == ca.CODEX_REDIRECT_URI


def test_refresh_uses_json_body(monkeypatch):
    cap: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(200, {"access_token": "AT2"}), cap)
    tok = asyncio.run(ca.refresh_access_token("RT"))
    assert tok["access_token"] == "AT2"
    assert cap["data"] is None  # JSON body, not form
    assert cap["json"] == {"client_id": ca.CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": "RT"}


@pytest.mark.parametrize("marker", sorted(ca.UNRECOVERABLE_REFRESH_ERRORS))
def test_refresh_unrecoverable_classification(monkeypatch, marker):
    cap: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(400, {"error": marker}), cap)
    with pytest.raises(ca.CodexReauthRequired):
        asyncio.run(ca.refresh_access_token("RT"))


def test_refresh_unrecoverable_from_raw_text(monkeypatch):
    cap: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(400, None, text="error: refresh_token_expired"), cap)
    with pytest.raises(ca.CodexReauthRequired):
        asyncio.run(ca.refresh_access_token("RT"))


def test_refresh_transient_5xx(monkeypatch):
    cap: dict = {}
    _patch_httpx(monkeypatch, _FakeResp(503, None, text="upstream boom"), cap)
    with pytest.raises(ca.CodexAuthError) as ei:
        asyncio.run(ca.refresh_access_token("RT"))
    assert not isinstance(ei.value, ca.CodexReauthRequired)


# ── persistence ─────────────────────────────────────────────────────────────

def test_persist_writes_all_keys_with_secret_flags():
    st = FakeConfigStorage()
    tok = {
        "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
        "id_token": _make_jwt({
            "email": "a@b.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1", "chatgpt_plan_type": "plus"},
        }),
    }
    creds = ca.persist_token_response(st, "admin", tok, now=1000.0)
    assert creds.access_token == "AT" and creds.account_id == "acc-1"
    assert creds.plan_type == "plus" and creds.email == "a@b.com"
    assert st.rows[("admin", "openai.oauth_token")] == ("AT", True)
    assert st.rows[("admin", "openai.oauth_refresh_token")] == ("RT", True)
    assert st.rows[("admin", "openai.oauth_expires_at")] == ("4600", False)
    assert st.rows[("admin", "openai.oauth_account_id")] == ("acc-1", False)
    assert st.rows[("admin", "openai.oauth_last_refresh_at")] == ("1000", False)


def test_persist_keeps_old_refresh_token_on_rotation_omit():
    st = FakeConfigStorage()
    st.set("llm_config", "openai.oauth_refresh_token", "OLD-RT", is_secret=True)
    st.set("llm_config", "openai.oauth_account_id", "acc-1")
    tok = {"access_token": "AT-NEW", "expires_in": 3600}  # no refresh_token, no id_token
    creds = ca.persist_token_response(st, "admin", tok, now=1000.0)
    assert st.rows[("admin", "openai.oauth_refresh_token")] == ("OLD-RT", True)
    assert creds.account_id == "acc-1"  # preserved from storage


def test_persist_requires_account_id():
    st = FakeConfigStorage()
    with pytest.raises(ca.CodexAuthError):
        ca.persist_token_response(st, "admin", {"access_token": "AT"}, now=1000.0)


# ── get_valid_access_token ──────────────────────────────────────────────────

def _seed_valid(st, *, expires_in_from_now, last_refresh_age):
    now = time.time()
    st.set("llm_config", "openai.oauth_token", "AT", is_secret=True)
    st.set("llm_config", "openai.oauth_refresh_token", "RT", is_secret=True)
    st.set("llm_config", "openai.oauth_account_id", "acc-1")
    st.set("llm_config", "openai.oauth_plan_type", "plus")
    st.set("llm_config", "openai.oauth_email", "a@b.com")
    st.set("llm_config", "openai.oauth_expires_at", str(int(now + expires_in_from_now)))
    st.set("llm_config", "openai.oauth_last_refresh_at", str(int(now - last_refresh_age)))


def test_get_valid_returns_without_refresh_when_fresh(monkeypatch):
    ca._refresh_locks.clear()
    st = FakeConfigStorage()
    _seed_valid(st, expires_in_from_now=30 * 86400, last_refresh_age=3600)

    async def _boom(_rt):
        raise AssertionError("refresh must not be called for a fresh token")
    monkeypatch.setattr(ca, "refresh_access_token", _boom)

    creds = asyncio.run(ca.get_valid_access_token(st, "admin"))
    assert creds.access_token == "AT" and creds.account_id == "acc-1"


def test_get_valid_refreshes_within_lead(monkeypatch):
    ca._refresh_locks.clear()
    st = FakeConfigStorage()
    _seed_valid(st, expires_in_from_now=3600, last_refresh_age=3600)  # < 5-day lead

    async def _refresh(_rt):
        return {"access_token": "AT-NEW", "refresh_token": "RT-NEW", "expires_in": 3600}
    monkeypatch.setattr(ca, "refresh_access_token", _refresh)

    creds = asyncio.run(ca.get_valid_access_token(st, "admin"))
    assert creds.access_token == "AT-NEW"
    assert st.rows[("admin", "openai.oauth_token")][0] == "AT-NEW"


def test_get_valid_refreshes_when_stale(monkeypatch):
    ca._refresh_locks.clear()
    st = FakeConfigStorage()
    _seed_valid(st, expires_in_from_now=30 * 86400, last_refresh_age=9 * 86400)  # > 8-day staleness

    called = {"n": 0}

    async def _refresh(_rt):
        called["n"] += 1
        return {"access_token": "AT-NEW", "expires_in": 3600}
    monkeypatch.setattr(ca, "refresh_access_token", _refresh)

    creds = asyncio.run(ca.get_valid_access_token(st, "admin"))
    assert called["n"] == 1 and creds.access_token == "AT-NEW"


def test_get_valid_missing_refresh_raises_reauth():
    ca._refresh_locks.clear()
    st = FakeConfigStorage()
    st.set("llm_config", "openai.oauth_token", "AT", is_secret=True)  # legacy pasted token only
    with pytest.raises(ca.CodexReauthRequired):
        asyncio.run(ca.get_valid_access_token(st, "admin"))


def test_get_valid_transient_failure_keeps_valid_token(monkeypatch):
    ca._refresh_locks.clear()
    st = FakeConfigStorage()
    # expiry within lead (needs refresh) but still comfortably valid (>60s away)
    _seed_valid(st, expires_in_from_now=3600, last_refresh_age=3600)

    async def _refresh(_rt):
        raise ca.CodexAuthError("network down")
    monkeypatch.setattr(ca, "refresh_access_token", _refresh)

    creds = asyncio.run(ca.get_valid_access_token(st, "admin"))
    assert creds.access_token == "AT"  # fell back to the still-valid token


def test_get_valid_single_flight(monkeypatch):
    ca._refresh_locks.clear()
    st = FakeConfigStorage()
    _seed_valid(st, expires_in_from_now=3600, last_refresh_age=3600)

    calls = {"n": 0}

    async def _refresh(_rt):
        calls["n"] += 1
        await asyncio.sleep(0.05)  # hold the lock so the sibling queues
        # Long-lived refreshed token → the queued sibling's re-check sees a
        # token well outside the 5-day lead and skips its own refresh.
        return {"access_token": "AT-NEW", "refresh_token": "RT-NEW", "expires_in": 30 * 86400}
    monkeypatch.setattr(ca, "refresh_access_token", _refresh)

    async def _run():
        return await asyncio.gather(
            ca.get_valid_access_token(st, "admin"),
            ca.get_valid_access_token(st, "admin"),
        )

    results = asyncio.run(_run())
    assert calls["n"] == 1  # single-flight: only one upstream refresh
    assert all(r.access_token == "AT-NEW" for r in results)
