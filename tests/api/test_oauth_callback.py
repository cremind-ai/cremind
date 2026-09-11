"""Backend OAuth callback routes (app/api/oauth_callback.py).

The former standalone loopback listener (a dedicated callback port) was replaced
by always-running backend routes whose redirect is derived from APP_URL. The
Google/Atlassian skills (run as subprocesses) get their consent redirect
captured into a per-state inbox file; Calendar exchanges in-process; Drive
records the picked files.

Every handler does its work first and then 303s to the SPA's return view with a
one-time ref (app/api/oauth_return.py) — never the code or the state.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.api.oauth_callback as oc
import app.api.oauth_return as oret
import app.calendar.google_auth as ga
import app.drive.grant_flow as gf

_STATE = "yVuZU8nVnlXUnirYSBheNCnasvVPub"  # 30 chars — matches _STATE_RE
_CODE = "4/0AVGzR1CsecretAuthorizationCode"


def _req(query: str, params: dict) -> SimpleNamespace:
    """Minimal stand-in for a Starlette Request (query_params + url.query)."""
    return SimpleNamespace(query_params=params, url=SimpleNamespace(query=query))


def _set_system_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(oc.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)


@pytest.fixture
def system_dir(monkeypatch, tmp_path):
    _set_system_dir(monkeypatch, tmp_path)
    return tmp_path


def _returned(resp) -> dict:
    """Follow the 303 the way the SPA would: consume the ref it carries."""
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/#/oauth-return?ref=")
    # The return URL is the one thing the browser keeps in history and may send
    # as a Referer: it must carry nothing that could be replayed.
    assert "code=" not in location and "state=" not in location
    assert _STATE not in location and _CODE not in location
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    return oret.consume(location.split("ref=", 1)[1])


def _run(handler, query: str, params: dict):
    return asyncio.run(handler(_req(query, params)))


# ── /api/oauth/callback (the subprocess skills) ─────────────────────────────

def test_inbox_callback_writes_inbox(system_dir, monkeypatch):
    query = f"state={_STATE}&code=4%2Fabc&scope=email+openid"
    dst = system_dir / "oauth_inbox" / f"{_STATE}.txt"
    seen = []
    real_complete = oret.complete

    def complete_after_the_inbox(state, **kwargs):
        # Jira/Confluence share this route and the waiting link polls the file:
        # it must be on disk before anything about the browser's way back runs.
        seen.append(dst.exists())
        return real_complete(state, **kwargs)

    monkeypatch.setattr(oret, "complete", complete_after_the_inbox)
    resp = _run(oc._handle_inbox_callback, query, {"state": _STATE, "code": "4/abc"})
    assert seen == [True]
    assert dst.read_text(encoding="utf-8") == query
    assert not (system_dir / "oauth_inbox" / f"{_STATE}.txt.tmp").exists()
    result = _returned(resp)
    assert (result["outcome"], result["flow"]) == ("received", "skill")


def test_inbox_callback_rejects_bad_state(system_dir):
    resp = _run(oc._handle_inbox_callback, "state=bad+space", {"state": "bad space"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/#/oauth-return?error=invalid_state"
    inbox = system_dir / "oauth_inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


@pytest.mark.parametrize("handler", [
    oc._handle_inbox_callback, oc._handle_google_calendar_callback, oc._handle_google_drive_callback,
])
def test_a_state_with_a_trailing_newline_is_invalid(system_dir, handler):
    """``$`` alone matches before a trailing newline; the state is a filename."""
    state = _STATE + "\n"
    resp = _run(handler, f"state={_STATE}%0A&code=x", {"state": state, "code": "x"})
    assert resp.headers["location"] == "/#/oauth-return?error=invalid_state"
    inbox = system_dir / "oauth_inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


def test_inbox_callback_consent_error_still_captures(system_dir):
    """A denied consent (``error=...``) is still written so the waiting skill can
    surface the failure; the browser is told it was denied."""
    query = f"error=access_denied&state={_STATE}"
    resp = _run(oc._handle_inbox_callback, query, {"state": _STATE, "error": "access_denied"})
    assert (system_dir / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == query
    result = _returned(resp)
    assert result["outcome"] == "denied"
    assert "access_denied" in result["message"]


def test_a_denial_code_that_is_not_a_token_is_not_echoed(system_dir):
    query = f"error=%3Cscript%3E&state={_STATE}"
    resp = _run(oc._handle_inbox_callback, query, {"state": _STATE, "error": "<script>"})
    result = _returned(resp)
    assert result["outcome"] == "denied"
    assert "<script>" not in result["message"]


def test_an_atlassian_shaped_response_still_lands_in_the_inbox(system_dir):
    """Jira/Confluence send code + state only (no scope) on the same route."""
    query = f"code=abc123&state={_STATE}"
    resp = _run(oc._handle_inbox_callback, query, {"code": "abc123", "state": _STATE})
    assert (system_dir / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == query
    assert _returned(resp)["outcome"] == "received"


def test_an_inbox_write_failure_is_reported_as_failed(system_dir, monkeypatch):
    def refuse(state, query):
        raise OSError("read-only file system")

    monkeypatch.setattr(oc, "_write_inbox", refuse)
    resp = _run(oc._handle_inbox_callback, f"state={_STATE}&code=x", {"state": _STATE, "code": "x"})
    assert _returned(resp)["outcome"] == "failed"


def test_the_recorded_page_comes_back_with_the_outcome(system_dir):
    oret.record_context(_STATE, profile="alice", route="/alice/c/abc", flow="skill")
    resp = _run(oc._handle_inbox_callback, f"state={_STATE}&code=x", {"state": _STATE, "code": "x"})
    result = _returned(resp)
    assert (result["profile"], result["route"], result["outcome"]) == ("alice", "/alice/c/abc", "received")


def test_a_broken_return_store_falls_back_to_a_plain_page(system_dir, monkeypatch):
    """The callback's own work is done; an unwritable store must not hide that."""
    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(oret, "complete", broken)
    ok = _run(oc._handle_inbox_callback, f"state={_STATE}&code=x", {"state": _STATE, "code": "x"})
    assert ok.status_code == 200
    assert b"Response received" in ok.body
    assert (system_dir / "oauth_inbox" / f"{_STATE}.txt").exists()
    denied = _run(oc._handle_inbox_callback, f"state={_STATE}&error=access_denied",
                  {"state": _STATE, "error": "access_denied"})
    assert denied.status_code == 200
    assert b"did not complete" in denied.body


# ── /api/oauth/google-calendar/callback ─────────────────────────────────────

def _calendar(query_params: dict):
    query = "&".join(f"{k}={v}" for k, v in query_params.items())
    return _run(oc._handle_google_calendar_callback, query, query_params)


def test_calendar_success_exchanges_then_returns(system_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(ga, "complete_callback", lambda state, code: calls.append((state, code)))
    oret.record_context(_STATE, profile="alice", route="/alice/calendar", flow="calendar")
    result = _returned(_calendar({"state": _STATE, "code": _CODE}))
    assert calls == [(_STATE, _CODE)]
    assert result == {"outcome": "received", "flow": "calendar", "profile": "alice",
                      "route": "/alice/calendar", "message": None}


def test_calendar_denial_never_exchanges(system_dir, monkeypatch):
    def boom(state, code):
        raise AssertionError("a denied consent has no code to exchange")

    monkeypatch.setattr(ga, "complete_callback", boom)
    result = _returned(_calendar({"state": _STATE, "error": "access_denied"}))
    assert result["outcome"] == "denied"


def test_calendar_without_a_code_failed(system_dir, monkeypatch):
    monkeypatch.setattr(ga, "complete_callback", lambda state, code: None)
    assert _returned(_calendar({"state": _STATE}))["outcome"] == "failed"


def test_calendar_unknown_state_is_invalid(system_dir, monkeypatch):
    def unknown(state, code):
        raise ga.GoogleAuthError("unknown or expired OAuth state")

    monkeypatch.setattr(ga, "complete_callback", unknown)
    assert _returned(_calendar({"state": _STATE, "code": _CODE}))["outcome"] == "invalid"


@pytest.mark.parametrize("error", [
    ga.GoogleAuthError("token exchange failed: Client error '400 Bad Request' secret-detail"),
    RuntimeError("auth-token storage not ready secret-detail"),
])
def test_calendar_exchange_failure_is_failed_without_leaking_the_error(system_dir, monkeypatch, error):
    def fail(state, code):
        raise error

    monkeypatch.setattr(ga, "complete_callback", fail)
    result = _returned(_calendar({"state": _STATE, "code": _CODE}))
    assert result["outcome"] == "failed"
    assert "secret-detail" not in (result["message"] or "")


def test_calendar_bad_state_never_exchanges(system_dir, monkeypatch):
    def boom(state, code):
        raise AssertionError("a malformed state must not reach the exchange")

    monkeypatch.setattr(ga, "complete_callback", boom)
    resp = _calendar({"state": "../x", "code": _CODE})
    assert resp.headers["location"] == "/#/oauth-return?error=invalid_state"


# ── /api/oauth/google-drive/callback ────────────────────────────────────────

def _drive(query_params: dict):
    query = "&".join(f"{k}={v}" for k, v in query_params.items())
    return _run(oc._handle_google_drive_callback, query, query_params)


def test_drive_captured_is_received(system_dir, monkeypatch):
    seen = []

    def record(query):
        seen.append(query)
        return {"profile": "alice", "state": _STATE, "status": "captured"}

    monkeypatch.setattr(gf, "record_redirect", record)
    oret.record_context(_STATE, profile="alice", route="/alice/settings/gsuite", flow="drive")
    result = _returned(_drive({"state": _STATE, "picked_file_ids": "f1,f2"}))
    assert seen == [f"state={_STATE}&picked_file_ids=f1,f2"]
    assert (result["outcome"], result["flow"], result["route"]) == ("received", "drive", "/alice/settings/gsuite")


def test_drive_error_is_denied(system_dir, monkeypatch):
    monkeypatch.setattr(gf, "record_redirect",
                        lambda query: {"profile": "alice", "state": _STATE, "status": "error"})
    assert _returned(_drive({"state": _STATE, "error": "access_denied"}))["outcome"] == "denied"


def test_drive_unknown_round_is_invalid(system_dir, monkeypatch):
    def unknown(query):
        raise gf.DriveGrantError("unknown or expired grant state")

    monkeypatch.setattr(gf, "record_redirect", unknown)
    assert _returned(_drive({"state": _STATE, "picked_file_ids": "f1"}))["outcome"] == "invalid"


def test_drive_unexpected_error_is_failed(system_dir, monkeypatch):
    def broken(query):
        raise KeyError("pending")

    monkeypatch.setattr(gf, "record_redirect", broken)
    assert _returned(_drive({"state": _STATE}))["outcome"] == "failed"


def test_drive_bad_state_is_redirected_as_invalid(system_dir):
    resp = _drive({"state": "no"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/#/oauth-return?error=invalid_state"


def test_the_three_callbacks_are_get_only():
    routes = {route.path: route.methods for route in oc.get_oauth_callback_routes()}
    assert set(routes) == {
        "/api/oauth/callback",
        "/api/oauth/google-calendar/callback",
        "/api/oauth/google-drive/callback",
    }
    for methods in routes.values():
        assert methods <= {"GET", "HEAD"}
