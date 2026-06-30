"""Backend OAuth callback routes (app/api/oauth_callback.py).

The former standalone loopback listener (a dedicated callback port) was replaced
by always-running backend routes whose redirect is derived from APP_URL. The
Google/Atlassian skills (run as subprocesses) get their consent redirect
captured into a per-state inbox file.
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
