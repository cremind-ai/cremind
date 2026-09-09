"""Unit tests for the Codex device-code sign-in session store.

Drives :mod:`app.tools.builtin.codex_login` against a fake ``openai_codex``
whose device-code handle blocks on an ``asyncio.Event``, so a test controls
exactly when the user "confirms in the browser" - the one thing the real flow
waits up to fifteen minutes for.

Also covers the two sign-out halves that belong with it: ``codex_runner.logout``
(which must leave no ``auth.json`` behind) and ``codex_runner.find_cli`` (which
decides what a terminal sign-in would actually run).

Tests drive coroutines with ``asyncio.run`` (matching the repo's other tool
tests - no pytest-asyncio needed).
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import app.tools.builtin.codex_login as login
import app.tools.builtin.codex_runner as runner


class FakeDeviceCodeHandle:
    """The SDK's ``AsyncDeviceCodeLoginHandle``, with the wait under our thumb.

    ``wait()`` blocks on ``released`` until a test sets it, which is what makes
    the pending / success / cancel / timeout paths reachable deterministically
    instead of by sleeping.
    """

    def __init__(self, *, success: bool = True, error: Optional[str] = None):
        self.login_id = "sdk-login-1"
        self.verification_url = "https://auth.openai.com/device"
        self.user_code = "ABCD-EFGH"
        self.released = asyncio.Event()
        self.cancelled = False
        self._note = SimpleNamespace(success=success, error=error, login_id=self.login_id)

    async def wait(self):
        await self.released.wait()
        return self._note

    async def cancel(self):
        # Deliberately does NOT release the wait: the real cancel is an RPC and
        # the waiter is unblocked by the task cancellation that follows it.
        self.cancelled = True


class WedgedCancelHandle(FakeDeviceCodeHandle):
    """A handle whose ``cancel()`` never answers, like a dead app-server.

    The SDK's cancel is a JSON-RPC round trip whose reply is read off an
    unbounded queue in a worker thread, so a child process that has stopped
    answering never completes the await at all. This is the shape that used to
    wedge the sign-in request itself, because the cancel is on the request path
    whenever a second sign-in supersedes the first.
    """

    def __init__(self):
        super().__init__()
        self.cancel_entered = asyncio.Event()

    async def cancel(self):
        self.cancel_entered.set()
        await asyncio.Event().wait()


def _install_login_sdk(monkeypatch, *, handle=None, account_resp=None):
    """Install a fake ``openai_codex`` exposing the login/logout surface."""

    mod = types.ModuleType("openai_codex")

    @dataclass
    class CodexConfig:
        codex_bin: Any = None
        config_overrides: tuple = ()
        cwd: Any = None
        env: Any = None

    class AsyncCodex:
        instances: list = []

        def __init__(self, config=None):
            self.config = config
            self.logged_out = False
            AsyncCodex.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def login_api_key(self, key):
            self.logged_in_key = key

        async def login_chatgpt_device_code(self):
            return handle

        async def account(self):
            return account_resp

        async def logout(self):
            self.logged_out = True

    mod.CodexConfig = CodexConfig
    mod.AsyncCodex = AsyncCodex
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point both CLI homes at ``tmp_path`` and empty the session registries.

    A session left over from one test would be the "previous" session the next
    test's :func:`start` cancels, so the registries are cleared on both sides.
    """
    from app.config import runtime_env

    login._reset_for_tests()
    runner._models_cache.clear()
    monkeypatch.setattr(runner.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path / "system"))
    monkeypatch.setattr(runtime_env, "is_container", lambda *a, **k: False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "shared-codex"))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield
    login._reset_for_tests()
    runner._models_cache.clear()


def _chatgpt_account(email="lee@example.com", plan="pro"):
    return SimpleNamespace(
        account=SimpleNamespace(
            root=SimpleNamespace(type="chatgpt", email=email, plan_type=plan),
        ),
        requires_openai_auth=True,
    )


# -- the happy path --------------------------------------------------------
def test_start_waits_in_the_profile_home_and_reports_the_account(monkeypatch):
    """The code is shown while pending; the credential lands in the PROFILE home.

    ``CODEX_HOME`` has to be in the config env before ``AsyncCodex`` is entered
    - the app-server reads (and rewrites) ``auth.json`` once, at spawn - so a
    home named any later would sign the user into the shared home and overwrite
    the operator's login.
    """
    handle = FakeDeviceCodeHandle()
    mod = _install_login_sdk(monkeypatch, handle=handle, account_resp=_chatgpt_account())
    home = runner.profile_codex_home("lee")
    # Seed the per-home model cache so the sign-in is shown to drop it: the list
    # belongs to the account that was signed in a moment ago.
    key = runner._cache_key(runner.CodexAuth(env_overrides={"CODEX_HOME": str(home)}))
    runner._models_cache[key] = (0.0, [{"id": "stale"}])

    async def body():
        session = await login.start("lee", {})
        assert session.status == "pending"
        assert session.verification_url == "https://auth.openai.com/device"
        assert session.user_code == "ABCD-EFGH"
        assert session.home == str(home)
        assert mod.AsyncCodex.instances[0].config.env["CODEX_HOME"] == str(home)
        # Readable by id while it waits - that is what the card polls.
        assert login.get(session.login_id) is session

        handle.released.set()  # the user confirms in the browser
        await asyncio.wait_for(session.task, timeout=5)
        assert session.status == "success"
        assert session.account == {
            "type": "chatgpt", "email": "lee@example.com", "plan_type": "pro",
        }
        assert session.finished_at is not None
        assert session.public()["status"] == "success"

    asyncio.run(body())
    assert key not in runner._models_cache


def test_a_refused_login_becomes_an_error_with_the_reason(monkeypatch):
    handle = FakeDeviceCodeHandle(success=False, error="access_denied")
    _install_login_sdk(monkeypatch, handle=handle)

    async def body():
        session = await login.start("lee", {})
        handle.released.set()
        await asyncio.wait_for(session.task, timeout=5)
        assert session.status == "error"
        assert "access_denied" in session.detail
        assert session.account is None

    asyncio.run(body())


# -- cancellation ------------------------------------------------------------
def test_cancel_tells_the_app_server_to_abandon_the_attempt(monkeypatch):
    """Cancelling must cancel the LOGIN, not just forget the session.

    The device code stays live upstream otherwise, and the app-server child
    keeps holding it - which is the leak the shutdown hook and the profile
    delete both call this for.
    """
    handle = FakeDeviceCodeHandle()
    _install_login_sdk(monkeypatch, handle=handle)

    async def body():
        session = await login.start("lee", {})
        assert await login.cancel(session.login_id) is True
        assert handle.cancelled is True
        assert session.status == "cancelled"
        assert session.task.done()
        # Already terminal: a second cancel is a no-op, not a second RPC.
        assert await login.cancel(session.login_id) is False

    asyncio.run(body())


def test_cancel_for_profile_stops_that_profile_s_pending_sign_in(monkeypatch):
    handle = FakeDeviceCodeHandle()
    _install_login_sdk(monkeypatch, handle=handle)

    async def body():
        session = await login.start("lee", {})
        assert await login.cancel_for_profile("sam") is False  # nothing pending
        assert await login.cancel_for_profile("lee") is True
        assert session.status == "cancelled"
        assert handle.cancelled is True

    asyncio.run(body())


def test_close_all_cancels_every_pending_sign_in(monkeypatch):
    handle = FakeDeviceCodeHandle()
    _install_login_sdk(monkeypatch, handle=handle)

    async def body():
        session = await login.start("lee", {})
        await login.close_all()
        assert session.status == "cancelled"
        assert handle.cancelled is True

    asyncio.run(body())


def test_a_second_start_cancels_the_first(monkeypatch):
    """Two live codes for one profile is not a state anyone can act on: only
    one of them can write ``auth.json``, and the card shows the newer one."""
    first = FakeDeviceCodeHandle()
    second = FakeDeviceCodeHandle()
    handles = [first, second]
    mod = _install_login_sdk(monkeypatch, handle=None)

    async def _next_handle(self):
        return handles.pop(0)

    mod.AsyncCodex.login_chatgpt_device_code = _next_handle

    async def body():
        one = await login.start("lee", {})
        two = await login.start("lee", {})
        assert one.login_id != two.login_id
        assert one.status == "cancelled"
        assert first.cancelled is True
        assert two.status == "pending"
        assert login.get(one.login_id) is one  # still readable, so the UI can say why
        await login.cancel(two.login_id)

    asyncio.run(body())


def test_a_cancel_the_app_server_never_answers_still_ends_the_session(monkeypatch):
    """A silent app-server must not be able to hold the cancel open.

    Driven through ``asyncio.wait_for`` so losing the bound fails the test in
    seconds instead of hanging the suite.
    """
    handle = WedgedCancelHandle()
    _install_login_sdk(monkeypatch, handle=handle)
    monkeypatch.setattr(login, "_CANCEL_TIMEOUT", 0.05)

    async def body():
        session = await login.start("lee", {})
        assert session.status == "pending"
        assert await asyncio.wait_for(login.cancel(session.login_id), timeout=5) is True
        assert handle.cancel_entered.is_set()  # the RPC was sent; only the reply is missing
        assert session.status == "cancelled"
        assert session.task.done()

    asyncio.run(body())


def test_a_wedged_cancel_does_not_block_the_next_sign_in(monkeypatch):
    """The bug this bound exists for: ``start`` cancels the previous attempt
    while the HTTP request waits, so an unanswerable cancel showed up as a
    sign-in dialog stuck on "starting" with no way out."""
    first = WedgedCancelHandle()
    second = FakeDeviceCodeHandle()
    handles = [first, second]
    mod = _install_login_sdk(monkeypatch, handle=None)
    monkeypatch.setattr(login, "_CANCEL_TIMEOUT", 0.05)

    async def _next_handle(self):
        return handles.pop(0)

    mod.AsyncCodex.login_chatgpt_device_code = _next_handle

    async def body():
        one = await login.start("lee", {})
        two = await asyncio.wait_for(login.start("lee", {}), timeout=5)
        assert two.login_id != one.login_id
        assert two.status == "pending"
        assert two.user_code == "ABCD-EFGH"  # a live code for the NEW attempt
        assert one.status == "cancelled"
        await login.cancel(two.login_id)

    asyncio.run(body())


# -- timeouts ----------------------------------------------------------------
def test_an_unconfirmed_code_expires(monkeypatch):
    handle = FakeDeviceCodeHandle()
    _install_login_sdk(monkeypatch, handle=handle)
    monkeypatch.setattr(login, "_LOGIN_TIMEOUT", 0.05)

    async def body():
        session = await login.start("lee", {})
        assert session.status == "pending"
        await asyncio.wait_for(session.task, timeout=5)
        assert session.status == "error"
        assert "expired" in session.detail
        # The upstream attempt is abandoned too, not left holding the code.
        assert handle.cancelled is True

    asyncio.run(body())


def test_a_start_that_never_produces_a_code_fails_fast(monkeypatch):
    """The HTTP request only waits on the handshake, so it gets a short leash."""
    mod = _install_login_sdk(monkeypatch, handle=None)
    monkeypatch.setattr(login, "_START_TIMEOUT", 0.05)

    async def _hang(self):
        await asyncio.Event().wait()

    mod.AsyncCodex.login_chatgpt_device_code = _hang

    async def body():
        session = await login.start("lee", {})
        assert session.status == "error"
        assert "code" in session.detail

    asyncio.run(body())


def test_start_without_the_sdk_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", None)

    async def body():
        with pytest.raises(RuntimeError, match="openai_codex"):
            await login.start("lee", {})

    asyncio.run(body())


def test_get_on_an_unknown_id_is_none():
    """The signal the API turns into "sign-in interrupted, start again":
    sessions live in this process only, held open by a child process."""
    assert login.get("never-existed") is None


# -- sign-out ----------------------------------------------------------------
def _seed(home, payload='{"tokens": {"access_token": "x"}}'):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(payload, encoding="utf-8")
    return home


def test_logout_removes_the_profile_credential(monkeypatch, tmp_path):
    mod = _install_login_sdk(monkeypatch)
    home = _seed(runner.profile_codex_home("lee"))
    shared = _seed(tmp_path / "shared-codex")
    key = runner._cache_key(runner.CodexAuth(env_overrides={"CODEX_HOME": str(home)}))
    runner._models_cache[key] = (0.0, [{"id": "stale"}])

    out = asyncio.run(runner.logout({}, "lee", scope="profile"))
    assert out["ok"] is True
    assert out["home"] == str(home)
    assert not (home / "auth.json").exists()
    # The RPC ran against the profile's home, and the shared login is untouched.
    assert mod.AsyncCodex.instances[-1].logged_out is True
    assert mod.AsyncCodex.instances[-1].config.env["CODEX_HOME"] == str(home)
    assert (shared / "auth.json").exists()
    assert key not in runner._models_cache


def test_logout_shared_scope_signs_the_server_out(monkeypatch, tmp_path):
    _install_login_sdk(monkeypatch)
    shared = _seed(tmp_path / "shared-codex")
    mine = _seed(runner.profile_codex_home("lee"))

    out = asyncio.run(runner.logout({}, "lee", scope="shared"))
    assert out["ok"] is True
    assert not (shared / "auth.json").exists()
    assert (mine / "auth.json").exists()


def test_logout_removes_the_credential_even_without_the_sdk(monkeypatch):
    """The user asked to be signed out. Leaving a live OAuth refresh token on
    disk because an RPC could not run is the wrong failure mode."""
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    home = _seed(runner.profile_codex_home("lee"))

    out = asyncio.run(runner.logout({}, "lee", scope="profile"))
    assert out["ok"] is True
    assert not (home / "auth.json").exists()


def test_logout_on_a_home_with_no_login_is_a_no_op(monkeypatch):
    _install_login_sdk(monkeypatch)
    out = asyncio.run(runner.logout({}, "sam", scope="profile"))
    assert out["ok"] is True


# -- find_cli ----------------------------------------------------------------
def _fake_cli_bin(monkeypatch, path=None):
    """Stand in for the ``codex_cli_bin`` wheel: ``path`` or "not packaged"."""
    fake = types.ModuleType("codex_cli_bin")

    def _bundled():
        if path is None:
            raise FileNotFoundError("no packaged binary")
        return path

    fake.bundled_codex_path = _bundled
    monkeypatch.setitem(sys.modules, "codex_cli_bin", fake)
    return fake


def test_find_cli_prefers_the_tool_variable(monkeypatch):
    variables = {runner.Var.BIN_PATH: "/opt/codex"}
    assert runner.find_cli(variables) == "/opt/codex"
    assert runner.cli_binary_source(variables) == "tool_variable"
    # The device-code flow, never the plain `codex login`: a browser-and-
    # loopback login is nothing on a headless server.
    assert runner.login_argv("/opt/codex") == ["/opt/codex", "login", "--device-auth"]
    assert runner.logout_argv("/opt/codex") == ["/opt/codex", "logout"]
    assert runner.status_argv("/opt/codex") == ["/opt/codex", "login", "status"]


def test_find_cli_falls_back_to_the_bundled_binary(monkeypatch, tmp_path):
    """The bundled binary beats PATH: it is the one the SDK itself drives, so a
    login written by a different ``codex`` from PATH would never be read."""
    _fake_cli_bin(monkeypatch, tmp_path / "bundled" / "codex")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codex")
    assert runner.find_cli({}) == str(tmp_path / "bundled" / "codex")
    assert runner.cli_binary_source({}) == "bundled"


def test_find_cli_falls_back_to_path(monkeypatch):
    _fake_cli_bin(monkeypatch, None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codex")
    assert runner.find_cli({}) == "/usr/local/bin/codex"
    assert runner.cli_binary_source({}) == "path"


def test_find_cli_reports_nothing_when_there_is_no_binary(monkeypatch):
    _fake_cli_bin(monkeypatch, None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert runner.find_cli({}) is None
    assert runner.cli_binary_source({}) is None
    # ...and with no variables at all, which is how the API asks.
    assert runner.find_cli() is None
