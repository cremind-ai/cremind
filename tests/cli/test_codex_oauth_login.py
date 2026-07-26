"""CLI: `cremind llm codex-oauth login` races local and server-side capture.

The redirect URI is pinned to localhost:1455, so which side can catch it depends
entirely on where the browser runs relative to the server. The command listens on
both and finishes on whichever fires; these tests cover that orchestration with a
stubbed listener (the real socket handling lives in test_codex_listener.py).

``codex_oauth_login`` does function-body imports, so the client functions and the
listener class are patched at their defining modules (no network, no real port).
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from typer.testing import CliRunner

import app.cli.client.llm as llm_client
import app.cli.codex_listener as codex_listener
from app.cli.client.llm import CodexOAuthStart, CodexOAuthStatus
from app.cli.codex_listener import CallbackResult


STATE = "abcdef0123456789abcdef"
REDIRECT = f"http://localhost:1455/auth/callback?code=THECODE&state={STATE}"


def _start(*, listener_active: bool, capture_hint: str = "", deployment: str = "native"):
    return CodexOAuthStart(
        authorize_url="https://auth.openai.com/oauth/authorize?x=1",
        state=STATE,
        redirect_uri="http://localhost:1455/auth/callback",
        listener_active=listener_active,
        listener_error="Port 1455 is already in use" if not listener_active else "",
        expires_in=60,
        deployment=deployment,
        capture_hint=capture_hint,
    )


def _complete_status(email: str = "you@example.com") -> CodexOAuthStatus:
    return CodexOAuthStatus(status="complete", email=email, plan_type="plus",
                            account_id="acc-1", error="")


class FakeListener:
    """Stand-in for LocalCallbackListener with a scripted outcome."""

    def __init__(self, *, binds: bool, result: Optional[CallbackResult] = None,
                 delay: float = 0.0):
        self._binds = binds
        self._result = result
        self._delay = delay
        self.closed = False
        self.state: str = ""

    def factory(self, state: str, port: int = 1455):
        self.state = state
        return self

    async def start(self) -> bool:
        return self._binds

    async def wait(self) -> CallbackResult:
        if self._result is None:          # bound, but the redirect never arrives
            await asyncio.sleep(3600)
        await asyncio.sleep(self._delay)
        return self._result

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, *, start, listener: FakeListener,
           status=None, complete=None) -> dict:
    calls: dict = {"status_polls": 0, "complete_args": None}

    async def fake_start(client):
        return start

    async def fake_status(client, state):
        calls["status_polls"] += 1
        return status(calls["status_polls"]) if status else CodexOAuthStatus(
            status="pending", email="", plan_type="", account_id="", error="")

    async def fake_complete(client, redirect_url, state=None):
        calls["complete_args"] = (redirect_url, state)
        return complete or _complete_status()

    monkeypatch.setattr(llm_client, "codex_oauth_start", fake_start)
    monkeypatch.setattr(llm_client, "codex_oauth_status", fake_status)
    monkeypatch.setattr(llm_client, "codex_oauth_complete", fake_complete)
    monkeypatch.setattr(codex_listener, "LocalCallbackListener", listener.factory)
    return calls


def _invoke(*extra: str):
    from app.cli.main import app
    return CliRunner().invoke(
        app, ["--token", "t", "llm", "codex-oauth", "login", "--no-browser", *extra],
    )


def test_local_capture_relays_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remote-server case: only this machine can see the redirect."""
    listener = FakeListener(binds=True, result=CallbackResult(redirect_url=REDIRECT))
    calls = _patch(monkeypatch, start=_start(listener_active=False), listener=listener)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert calls["complete_args"] == (REDIRECT, STATE)
    assert calls["status_polls"] == 0        # no server listener to poll
    assert "you@example.com" in result.output
    assert listener.closed is True
    assert listener.state == STATE           # the flow's state, not a fresh one


def test_server_capture_when_local_port_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The native case: the server owns 1455, so the CLI just polls it."""
    listener = FakeListener(binds=False)

    def status(n):
        return _complete_status() if n >= 2 else CodexOAuthStatus(
            status="pending", email="", plan_type="", account_id="", error="")

    calls = _patch(monkeypatch, start=_start(listener_active=True),
                   listener=listener, status=status)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert calls["complete_args"] is None    # nothing relayed; server had it
    assert calls["status_polls"] >= 2
    assert listener.closed is True


def test_local_capture_wins_the_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both sides listening: the one the browser actually reached finishes it."""
    listener = FakeListener(binds=True, result=CallbackResult(redirect_url=REDIRECT))
    calls = _patch(monkeypatch, start=_start(listener_active=True), listener=listener)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert calls["complete_args"] == (REDIRECT, STATE)


def test_provider_error_from_local_capture_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = FakeListener(binds=True, result=CallbackResult(error="User declined"))
    calls = _patch(monkeypatch, start=_start(listener_active=False), listener=listener)

    result = _invoke()

    assert result.exit_code == 1
    assert "User declined" in result.output
    assert calls["complete_args"] is None


def test_server_error_status_fails_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = FakeListener(binds=False)

    def status(n):
        return CodexOAuthStatus(status="error", email="", plan_type="",
                                account_id="", error="token exchange failed")

    _patch(monkeypatch, start=_start(listener_active=True), listener=listener,
           status=status)

    result = _invoke()

    assert result.exit_code == 1
    assert "token exchange failed" in result.output


def test_paste_fallback_when_neither_side_can_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    """CliRunner's stdin is not a tty, so the command points at `complete`."""
    listener = FakeListener(binds=False)
    _patch(monkeypatch, start=_start(listener_active=False), listener=listener)

    result = _invoke()

    assert result.exit_code == 1
    assert "automatic capture unavailable" in result.output
    assert "Port 1455 is already in use" in result.output
    assert listener.closed is True


def test_capture_hint_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A containerized server tells the user what its deployment needs."""
    hint = "This is a Kubernetes install, so ... port-forward svc/cremind 1515:80 1455:1455"
    listener = FakeListener(binds=True, result=CallbackResult(redirect_url=REDIRECT))
    _patch(monkeypatch, start=_start(listener_active=True, capture_hint=hint,
                                     deployment="kubernetes"), listener=listener)

    result = _invoke()

    assert result.exit_code == 0, result.output
    assert "port-forward svc/cremind 1515:80 1455:1455" in result.output


def test_timeout_points_at_the_complete_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved on another machine: nothing arrives, so say what to do next."""
    listener = FakeListener(binds=True, result=None)   # binds, never fires
    _patch(monkeypatch, start=_start(listener_active=False), listener=listener)

    # The command floors its deadline at 60s, so jump the clock forward instead
    # of actually waiting it out: the first reading sets the deadline, the next
    # one is already past it, leaving asyncio.wait its 1s minimum.
    import app.cli.commands.llm as llm_cmd

    class JumpingClock:
        def __init__(self):
            self._reads = 0

        def monotonic(self) -> float:
            self._reads += 1
            return 0.0 if self._reads == 1 else 10_000.0

    monkeypatch.setattr(llm_cmd, "time", JumpingClock())

    result = _invoke()

    assert result.exit_code == 1
    assert "codex-oauth complete" in result.output
    assert listener.closed is True
