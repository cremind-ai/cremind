"""Backend OAuth callback routes (app/api/oauth_callback.py).

The former standalone loopback listener (a dedicated callback port) was replaced
by always-running backend routes whose redirect is derived from APP_URL. The
Google/Atlassian skills (run
as subprocesses) get their consent redirect captured into a per-state inbox
file; A2A tool auth (in-process) resolves an asyncio Future instead.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import app.api.oauth_callback as oc

_STATE = "yVuZU8nVnlXUnirYSBheNCnasvVPub"  # 30 chars — matches _STATE_RE


def _req(query: str, params: dict) -> SimpleNamespace:
    """Minimal stand-in for a Starlette Request (query_params + url.query)."""
    return SimpleNamespace(query_params=params, url=SimpleNamespace(query=query))


def _set_system_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(oc.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)


def test_inbox_callback_writes_inbox(monkeypatch, tmp_path):
    _set_system_dir(monkeypatch, tmp_path)
    query = f"state={_STATE}&code=4%2Fabc&scope=email+openid"
    resp = asyncio.run(oc._handle_inbox_callback(_req(query, {"state": _STATE, "code": "4/abc"})))
    assert resp.status_code == 200
    dst = tmp_path / "oauth_inbox" / f"{_STATE}.txt"
    assert dst.read_text(encoding="utf-8") == query
    assert not (tmp_path / "oauth_inbox" / f"{_STATE}.txt.tmp").exists()


def test_inbox_callback_rejects_bad_state(monkeypatch, tmp_path):
    _set_system_dir(monkeypatch, tmp_path)
    resp = asyncio.run(oc._handle_inbox_callback(_req("state=bad+space", {"state": "bad space"})))
    assert resp.status_code == 400
    inbox = tmp_path / "oauth_inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


def test_inbox_callback_consent_error_still_captures(monkeypatch, tmp_path):
    """A denied consent (``error=...``) is still written so the waiting skill can
    surface the failure; the route returns 200 with the error page."""
    _set_system_dir(monkeypatch, tmp_path)
    query = f"error=access_denied&state={_STATE}"
    resp = asyncio.run(oc._handle_inbox_callback(_req(query, {"state": _STATE, "error": "access_denied"})))
    assert resp.status_code == 200
    assert (tmp_path / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == query


def test_a2a_callback_resolves_future():
    from app.tools.a2a import oauth_rendezvous as rv

    async def scenario():
        # A2A keys by the raw state (base64 with '=' padding); no _STATE_RE.
        state = "a2a-state-AbCdEf=="
        fut = asyncio.get_running_loop().create_future()
        rv.register(state, fut)
        resp = await oc._handle_a2a_callback(
            _req(f"state={state}&code=THECODE", {"state": state, "code": "THECODE"})
        )
        assert resp.status_code == 200
        await asyncio.sleep(0)  # let call_soon_threadsafe deliver the result
        assert fut.result() == "THECODE"

    asyncio.run(scenario())


def test_a2a_callback_unknown_state_400():
    resp = asyncio.run(
        oc._handle_a2a_callback(_req("state=unknown123&code=x", {"state": "unknown123", "code": "x"}))
    )
    assert resp.status_code == 400


def test_a2a_callback_consent_error_resolves_none():
    from app.tools.a2a import oauth_rendezvous as rv

    async def scenario():
        state = "a2a-err-state-9090"
        fut = asyncio.get_running_loop().create_future()
        rv.register(state, fut)
        resp = await oc._handle_a2a_callback(
            _req(f"error=access_denied&state={state}", {"state": state, "error": "access_denied"})
        )
        assert resp.status_code == 200
        await asyncio.sleep(0)
        assert fut.result() is None  # consent error → None so authenticate() raises

    asyncio.run(scenario())
