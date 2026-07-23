"""Tests for the Codex OAuth endpoints (app/api/llm.py) and the in-process flow
orchestrator + loopback listener (app/api/llm_codex_flow.py).

Endpoint tests drive the handlers with a fake config store and a monkeypatched
listener (no real socket); one integration test binds a real ephemeral port and
drives a raw HTTP callback through the loopback listener end to end.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from app.api import llm as llm_api
from app.api import llm_codex_flow as flow
from app.lib.llm import codex_auth as ca


class FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, bool]] = {}

    def get(self, table: str, key: str, profile: str = "admin") -> Optional[str]:
        row = self.rows.get((profile, key))
        return row[0] if row else None

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin") -> None:
        self.rows[(profile, key)] = (value, is_secret)

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        return self.rows.pop((profile, key), None) is not None

    def delete_by_prefix(self, table: str, prefix: str, profile: str = "admin") -> int:
        victims = [k for k in self.rows if k[0] == profile and k[1].startswith(prefix)]
        for k in victims:
            del self.rows[k]
        return len(victims)


def _make_request(*, body: Any = None, query: Optional[dict] = None,
                  authenticated: bool = True, profile: str = "admin") -> object:
    async def _json() -> Any:
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        headers={}, cookies={},
        user=SimpleNamespace(is_authenticated=authenticated, username=profile),
        path_params={},
        query_params=(query or {}),
        json=_json,
    )


def _handlers(storage, monkeypatch) -> dict[tuple[str, str], Callable]:
    monkeypatch.setattr(llm_api, "require_auth_or_setup_mode", lambda req, cs: None)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda profile: None)
    state = SimpleNamespace(config_storage=storage)
    out: dict[tuple[str, str], Callable] = {}
    for r in llm_api.get_llm_routes(state):
        for m in r.methods:
            out[(r.path, m)] = r.endpoint
    return out


def _body(resp) -> dict:
    return json.loads(resp.body)


def _make_jwt(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{raw}.sig"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _reset_flow():
    flow._pending.clear()
    asyncio.run(flow.stop_listener())
    yield
    asyncio.run(flow.stop_listener())
    flow._pending.clear()


# ── start ───────────────────────────────────────────────────────────────────

def test_start_requires_auth(monkeypatch):
    h = _handlers(FakeConfigStorage(), monkeypatch)
    resp = asyncio.run(h[("/api/llm/auth/codex/start", "POST")](_make_request(authenticated=False)))
    assert resp.status_code == 401


def test_start_requires_storage(monkeypatch):
    monkeypatch.setattr(llm_api, "require_auth_or_setup_mode", lambda req, cs: None)
    state = SimpleNamespace(config_storage=None)
    handlers = {(r.path, m): r.endpoint for r in llm_api.get_llm_routes(state) for m in r.methods}
    resp = asyncio.run(handlers[("/api/llm/auth/codex/start", "POST")](_make_request()))
    assert resp.status_code == 503


def test_start_success_registers_state(monkeypatch):
    async def _fake_listener(port):
        return True, None
    monkeypatch.setattr(flow, "_start_listener", _fake_listener)
    h = _handlers(FakeConfigStorage(), monkeypatch)
    resp = asyncio.run(h[("/api/llm/auth/codex/start", "POST")](_make_request()))
    data = _body(resp)
    assert data["listener_active"] is True
    assert data["redirect_uri"] == ca.CODEX_REDIRECT_URI
    assert "auth.openai.com" in data["authorize_url"]
    assert data["state"] in flow._pending
    assert flow._pending[data["state"]]["profile"] == "admin"


def test_start_listener_bind_failure_still_succeeds(monkeypatch):
    async def _fake_listener(port):
        return False, "Port 1455 is already in use"
    monkeypatch.setattr(flow, "_start_listener", _fake_listener)
    h = _handlers(FakeConfigStorage(), monkeypatch)
    data = _body(asyncio.run(h[("/api/llm/auth/codex/start", "POST")](_make_request())))
    assert data["listener_active"] is False
    assert "1455" in data["listener_error"]
    assert data["authorize_url"]  # flow still usable via paste fallback


# ── status ────────────────────────────────────────────────────────────────

def test_status_invalid_state(monkeypatch):
    h = _handlers(FakeConfigStorage(), monkeypatch)
    resp = asyncio.run(h[("/api/llm/auth/codex/status", "GET")](_make_request(query={"state": "!!bad!!"})))
    assert resp.status_code == 400


def test_status_unknown_state_is_expired(monkeypatch):
    h = _handlers(FakeConfigStorage(), monkeypatch)
    resp = asyncio.run(h[("/api/llm/auth/codex/status", "GET")](
        _make_request(query={"state": "abcdefgh12345678"})))
    assert _body(resp)["status"] == "expired"


def test_status_foreign_profile_is_expired(monkeypatch):
    h = _handlers(FakeConfigStorage(), monkeypatch)
    flow._pending["mystate12345678"] = {"profile": "someone-else", "status": "pending", "created_at": 9e18}
    resp = asyncio.run(h[("/api/llm/auth/codex/status", "GET")](
        _make_request(query={"state": "mystate12345678"}, profile="admin")))
    assert _body(resp)["status"] == "expired"


# ── complete (paste fallback) ───────────────────────────────────────────────

def _register_pending(storage, state="pastestate123456"):
    flow._pending[state] = {
        "profile": "admin", "verifier": "verifier", "config_storage": storage,
        "created_at": 9e18, "status": "pending", "error": None,
        "email": None, "plan_type": None, "account_id": None,
    }
    return state


def test_complete_success_persists_tokens(monkeypatch):
    async def _fake_exchange(code, verifier):
        return {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            "id_token": _make_jwt({
                "email": "you@example.com",
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-9", "chatgpt_plan_type": "pro"},
            }),
        }
    monkeypatch.setattr(ca, "exchange_code", _fake_exchange)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda p: None)

    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    state = _register_pending(storage)
    url = f"http://localhost:1455/auth/callback?code=THECODE&state={state}"
    resp = asyncio.run(h[("/api/llm/auth/codex/complete", "POST")](
        _make_request(body={"redirect_url": url, "state": state})))
    data = _body(resp)
    assert data["status"] == "complete"
    assert data["email"] == "you@example.com" and data["plan_type"] == "pro"
    assert storage.get("llm_config", "openai.oauth_token") == "AT"
    assert storage.get("llm_config", "openai.auth_method") == "codex_oauth"
    assert storage.get("llm_config", "openai.oauth_account_id") == "acc-9"


def test_complete_reconciles_stale_model_group(monkeypatch):
    """Signing in with ChatGPT must clear a model group left pointing at an
    API-key-only OpenAI model (the origin of the documentation_search bug)."""
    async def _fake_exchange(code, verifier):
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                "id_token": _make_jwt({"account_id": "acc-1"})}
    monkeypatch.setattr(ca, "exchange_code", _fake_exchange)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda p: None)

    storage = FakeConfigStorage()
    storage.set("llm_config", "model_group.high", "github-copilot/gpt-4.1")
    storage.set("llm_config", "model_group.low", "openai/gpt-4.1-mini")  # api_key-only
    h = _handlers(storage, monkeypatch)
    state = _register_pending(storage)
    url = f"http://localhost:1455/auth/callback?code=THECODE&state={state}"
    resp = asyncio.run(h[("/api/llm/auth/codex/complete", "POST")](
        _make_request(body={"redirect_url": url, "state": state})))
    assert _body(resp)["status"] == "complete"
    # Stale incompatible low is cleared (falls back to high); high is untouched.
    assert storage.get("llm_config", "model_group.low") is None
    assert storage.get("llm_config", "model_group.high") == "github-copilot/gpt-4.1"


def test_complete_bare_query_accepted(monkeypatch):
    async def _fake_exchange(code, verifier):
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                "id_token": _make_jwt({"account_id": "acc-1"})}
    monkeypatch.setattr(ca, "exchange_code", _fake_exchange)
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    state = _register_pending(storage)
    resp = asyncio.run(h[("/api/llm/auth/codex/complete", "POST")](
        _make_request(body={"redirect_url": f"code=X&state={state}"})))
    assert _body(resp)["status"] == "complete"


def test_complete_consent_error(monkeypatch):
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    state = _register_pending(storage)
    url = f"http://localhost:1455/auth/callback?error=access_denied&error_description=User+declined&state={state}"
    data = _body(asyncio.run(h[("/api/llm/auth/codex/complete", "POST")](
        _make_request(body={"redirect_url": url}))))
    assert data["status"] == "error"
    assert "declined" in data["error"].lower()


def test_complete_state_mismatch(monkeypatch):
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    _register_pending(storage, state="realstate123456")
    url = "http://localhost:1455/auth/callback?code=X&state=realstate123456"
    data = _body(asyncio.run(h[("/api/llm/auth/codex/complete", "POST")](
        _make_request(body={"redirect_url": url, "state": "different12345"}))))
    assert data["status"] == "error"


def test_complete_unknown_state(monkeypatch):
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    url = "http://localhost:1455/auth/callback?code=X&state=nostate12345678"
    data = _body(asyncio.run(h[("/api/llm/auth/codex/complete", "POST")](
        _make_request(body={"redirect_url": url}))))
    assert data["status"] == "error"


# ── configured-computation regression (handle_list_providers) ────────────────

def test_list_providers_oauth_configured_flag(monkeypatch):
    storage = FakeConfigStorage()
    storage.set("llm_config", "openai.auth_method", "codex_oauth")
    h = _handlers(storage, monkeypatch)
    resp = asyncio.run(h[("/api/llm/providers", "GET")](_make_request()))
    openai = next(p for p in _body(resp)["providers"] if p["name"] == "openai")
    assert openai["configured"] is False  # no token yet → not vacuously true

    storage.set("llm_config", "openai.oauth_token", "AT", is_secret=True)
    resp2 = asyncio.run(h[("/api/llm/providers", "GET")](_make_request()))
    openai2 = next(p for p in _body(resp2)["providers"] if p["name"] == "openai")
    assert openai2["configured"] is True


# ── loopback listener (real ephemeral socket) ────────────────────────────────

async def _http_get(port: int, target: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=5.0)
    writer.close()
    return data.decode("utf-8", "replace")


def test_loopback_listener_captures_callback(monkeypatch):
    async def _fake_exchange(code, verifier):
        assert code == "THECODE"
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
                "id_token": _make_jwt({"account_id": "acc-1", "email": "a@b.com"})}
    monkeypatch.setattr(ca, "exchange_code", _fake_exchange)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda p: None)

    storage = FakeConfigStorage()
    port = _free_port()

    async def _run():
        start = await flow.start_flow(storage, "admin", port=port)
        assert start["listener_active"] is True
        state = start["state"]
        # main callback path
        page = await _http_get(port, f"/auth/callback?code=THECODE&state={state}")
        assert "Signed in to ChatGPT" in page
        assert flow.get_flow_status(state, "admin")["status"] == "complete"
        assert storage.get("llm_config", "openai.oauth_token") == "AT"

    asyncio.run(_run())


def test_loopback_listener_alias_and_404(monkeypatch):
    monkeypatch.setattr(ca, "exchange_code", None)  # unused: bad/unknown state paths only
    storage = FakeConfigStorage()
    port = _free_port()

    async def _run():
        await flow.start_flow(storage, "admin", port=port)
        # /callback alias with an unknown state → error page (not a crash/404)
        page = await _http_get(port, "/callback?code=X&state=unknownstate123")
        assert "Sign-in failed" in page
        # unrelated path → 404
        other = await _http_get(port, "/nope")
        assert "404" in other.split("\r\n", 1)[0]

    asyncio.run(_run())


def test_start_listener_bind_failure_real(monkeypatch):
    port = _free_port()

    async def _run():
        # Occupy the port first so the flow's listener can't bind it.
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
        try:
            start = await flow.start_flow(FakeConfigStorage(), "admin", port=port)
            assert start["listener_active"] is False
            assert start["listener_error"]
            assert start["authorize_url"]
        finally:
            blocker.close()
            await blocker.wait_closed()

    asyncio.run(_run())
