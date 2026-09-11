"""Backend OAuth callback routes (app/api/oauth_callback.py).

The former standalone loopback listener (a dedicated callback port) was replaced
by always-running backend routes whose redirect is derived from APP_URL. The
Google/Atlassian skills (run as subprocesses) get their consent redirect
captured into a per-state inbox file; Calendar exchanges in-process; Drive
records the picked files.

Every handler does its work first and then answers with the self-closing page in
app/api/oauth_close.py — which carries the outcome and nothing about the grant.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import app.api.oauth_callback as oc
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
    """Where the consent window is sent — the only thing a callback decides.

    What that destination then shows and closes belongs to
    tests/api/test_oauth_close.py; here the point is that the browser is moved
    OFF this URL (it holds a live authorization code) and that what travels is
    the outcome and nothing else.
    """
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/api/oauth/close?")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"
    for secret in (_CODE, _STATE, "code=", "state="):
        assert secret not in location
    query = {key: value[0] for key, value in parse_qs(urlsplit(location).query).items()}
    return {"outcome": query.get("o"), "flow": query.get("f"),
            "reason": query.get("r"), "error": query.get("e")}


def _run(handler, query: str, params: dict):
    return asyncio.run(handler(_req(query, params)))


# ── /api/oauth/callback (the subprocess skills) ─────────────────────────────

def test_inbox_callback_writes_inbox(system_dir, monkeypatch):
    query = f"state={_STATE}&code=4%2Fabc&scope=email+openid"
    dst = system_dir / "oauth_inbox" / f"{_STATE}.txt"
    seen = []
    real_redirect = oc.close_redirect

    def redirect_after_the_inbox(**kwargs):
        # Jira/Confluence share this route and the waiting link polls the file:
        # it must be on disk before anything the browser is told.
        seen.append(dst.exists())
        return real_redirect(**kwargs)

    monkeypatch.setattr(oc, "close_redirect", redirect_after_the_inbox)
    resp = _run(oc._handle_inbox_callback, query, {"state": _STATE, "code": "4/abc"})
    assert seen == [True]
    assert dst.read_text(encoding="utf-8") == query
    assert not (system_dir / "oauth_inbox" / f"{_STATE}.txt.tmp").exists()
    result = _returned(resp)
    assert (result["outcome"], result["flow"]) == ("received", "skill")


def test_inbox_callback_rejects_bad_state(system_dir):
    resp = _run(oc._handle_inbox_callback, "state=bad+space", {"state": "bad space"})
    result = _returned(resp)
    # Nothing to retry and nothing to tell a waiting page: no flow was named.
    assert (result["outcome"], result["flow"], result["reason"]) == ("invalid", None, "nostate")
    inbox = system_dir / "oauth_inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


@pytest.mark.parametrize("handler", [
    oc._handle_inbox_callback, oc._handle_google_calendar_callback, oc._handle_google_drive_callback,
])
def test_a_state_with_a_trailing_newline_is_invalid(system_dir, handler):
    """``$`` alone matches before a trailing newline; the state is a filename."""
    state = _STATE + "\n"
    resp = _run(handler, f"state={_STATE}%0A&code=x", {"state": state, "code": "x"})
    result = _returned(resp)
    assert result["outcome"] == "invalid"
    # The route proves the flow even when the state does not; only the shared
    # skills callback cannot say which one it was.
    expected = {"_handle_google_calendar_callback": "calendar",
                "_handle_google_drive_callback": "drive"}.get(handler.__name__)
    assert result["flow"] == expected
    inbox = system_dir / "oauth_inbox"
    assert not inbox.exists() or not any(inbox.iterdir())


def test_inbox_callback_consent_error_still_captures(system_dir):
    """A denied consent (``error=...``) is still written so the waiting skill can
    surface the failure; the browser is told it was denied."""
    query = f"error=access_denied&state={_STATE}"
    resp = _run(oc._handle_inbox_callback, query, {"state": _STATE, "error": "access_denied"})
    assert (system_dir / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == query
    result = _returned(resp)
    assert (result["outcome"], result["reason"], result["error"]) == ("denied", "denied", "access_denied")


def test_a_denial_code_that_is_not_a_token_is_not_echoed(system_dir):
    query = f"error=%3Cscript%3E&state={_STATE}"
    resp = _run(oc._handle_inbox_callback, query, {"state": _STATE, "error": "<script>"})
    result = _returned(resp)
    assert result["outcome"] == "denied"
    # A provider error that is not a token is not passed on at all.
    assert result["error"] is None


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


def test_a_captured_response_closes_its_own_window(system_dir):
    """What the user asked for: the consent window goes away by itself, and the
    page behind it hears about it on the channel."""
    resp = _run(oc._handle_inbox_callback, f"state={_STATE}&code=x", {"state": _STATE, "code": "x"})
    result = _returned(resp)
    assert (result["outcome"], result["flow"]) == ("received", "skill")


# ── /api/oauth/google-calendar/callback ─────────────────────────────────────

def _calendar(query_params: dict):
    query = "&".join(f"{k}={v}" for k, v in query_params.items())
    return _run(oc._handle_google_calendar_callback, query, query_params)


def test_calendar_success_exchanges_then_returns(system_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(ga, "complete_callback", lambda state, code: calls.append((state, code)))
    result = _returned(_calendar({"state": _STATE, "code": _CODE}))
    assert calls == [(_STATE, _CODE)]
    assert (result["outcome"], result["flow"]) == ("received", "calendar")


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
    resp = _calendar({"state": _STATE, "code": _CODE})
    result = _returned(resp)
    assert (result["outcome"], result["reason"]) == ("failed", "exchange")
    # The exception's text is logged, never handed to the browser.
    assert "secret-detail" not in resp.headers["location"]


def test_calendar_bad_state_never_exchanges(system_dir, monkeypatch):
    def boom(state, code):
        raise AssertionError("a malformed state must not reach the exchange")

    monkeypatch.setattr(ga, "complete_callback", boom)
    resp = _calendar({"state": "../x", "code": _CODE})
    assert _returned(resp)["outcome"] == "invalid"


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
    result = _returned(_drive({"state": _STATE, "picked_file_ids": "f1,f2"}))
    assert seen == [f"state={_STATE}&picked_file_ids=f1,f2"]
    assert (result["outcome"], result["flow"]) == ("received", "drive")


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


def test_drive_bad_state_is_reported_as_invalid(system_dir):
    assert _returned(_drive({"state": "no"}))["outcome"] == "invalid"


def test_the_three_callbacks_are_get_only():
    routes = {route.path: route.methods for route in oc.get_oauth_callback_routes()}
    assert set(routes) == {
        "/api/oauth/callback",
        "/api/oauth/google-calendar/callback",
        "/api/oauth/google-drive/callback",
    }
    for methods in routes.values():
        assert methods <= {"GET", "HEAD"}
