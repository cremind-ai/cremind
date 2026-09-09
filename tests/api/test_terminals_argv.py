"""``create_terminal``: the single door into the PTY terminal registry.

The Coding Agents sign-in flow gave the built-in terminal a second caller: it
runs a login binary (``claude auth login``, ``codex login --device-auth``)
under the same PTY with a forced ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``, so a
user with no shell access to the server can finish an interactive CLI login in
the browser. That only holds together if the argv path goes through the *same*
``create_terminal`` as the "New terminal" button: one registry, one per-profile
cap, one reap sweep, one WebSocket. A parallel spawn path would give sign-in
terminals no cap and leave them running at shutdown.

So this pins the extraction:

- an argv terminal passes the argv **and** the forced environment through to
  ``spawn_argv_pty`` verbatim, and is labelled with the binary and the caller's
  title (never a "Terminal N" number, which belongs to the user's own shells);
- ``argv=None`` still spawns the bare interactive shell exactly as before;
- the per-profile cap counts argv terminals, and the route still answers 409
  with the message the UI shows;
- a long-detached terminal is still reaped on create.

The PTY itself is faked: a real one would need a tty (and a login binary) on the
test box, and every assertion here is about the registration around it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

import app.api.terminals as terminals


class _ParkedReader:
    """A stdout reader that never yields and never EOFs.

    ``_pump_output`` would otherwise see EOF on the first read, declare the
    terminal exited and pop it out of the registry before the assertions run.
    """

    def __init__(self) -> None:
        self._never = asyncio.Event()

    async def read(self, _n: int) -> bytes:  # pragma: no cover - awaited, never returns
        await self._never.wait()
        return b""


class _FakePty:
    """The subset of ``PtyProcess`` the registry and the pump touch."""

    def __init__(self) -> None:
        self.pid = 4242
        self.stdout = _ParkedReader()
        self.terminated = False
        self.killed = False
        self.closed = False

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def close_master(self) -> None:
        self.closed = True


class _Spawner:
    """Records how each spawn helper was called and hands back a fake PTY."""

    def __init__(self) -> None:
        self.argv_calls: List[Dict[str, Any]] = []
        self.shell_calls: List[Dict[str, Any]] = []
        self.processes: List[_FakePty] = []

    def _new(self) -> _FakePty:
        proc = _FakePty()
        self.processes.append(proc)
        return proc

    async def spawn_argv(
        self,
        argv: List[str],
        working_dir: str,
        *,
        cols: int = 80,
        rows: int = 24,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> _FakePty:
        self.argv_calls.append({
            "argv": argv,
            "working_dir": working_dir,
            "cols": cols,
            "rows": rows,
            "extra_env": extra_env,
        })
        return self._new()

    async def spawn_shell(
        self,
        working_dir: str,
        *,
        cols: int = 80,
        rows: int = 24,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> "tuple[_FakePty, str]":
        self.shell_calls.append({
            "working_dir": working_dir,
            "cols": cols,
            "rows": rows,
            "extra_env": extra_env,
        })
        return self._new(), "bash"


@pytest.fixture
def spawner(monkeypatch) -> _Spawner:
    """Replace both spawn helpers and leave the registry clean afterwards."""
    fake = _Spawner()
    monkeypatch.setattr(terminals, "spawn_argv_pty", fake.spawn_argv)
    monkeypatch.setattr(terminals, "spawn_interactive_shell_pty", fake.spawn_shell)
    terminals._terminal_registry.clear()
    terminals._title_counters.clear()
    yield fake
    terminals._terminal_registry.clear()
    terminals._title_counters.clear()


def _run(body: Callable[[], Any]) -> Any:
    """Run an async test body, then stop the pumps it left behind.

    ``create_terminal`` starts a pump task per terminal and the fake PTY parks
    it forever, so the tasks have to be cancelled *inside* the same event loop,
    or ``asyncio.run`` tears the loop down with pending tasks.
    """

    async def _wrapped() -> Any:
        try:
            return await body()
        finally:
            tasks = [
                info.pump_task
                for info in list(terminals._terminal_registry.values())
                if info.pump_task is not None
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    return asyncio.run(_wrapped())


# ---------------------------------------------------------------------------
# argv terminals (the Coding Agents sign-in path)
# ---------------------------------------------------------------------------

def test_argv_terminal_runs_the_binary_with_the_forced_credential_home(spawner):
    """The whole point of the argv path: the login binary is exec'd directly
    and the credential home rides in its environment. Going through a shell
    would let a user's shell profile re-export CLAUDE_CONFIG_DIR and land the
    login somewhere else."""
    env = {"CLAUDE_CONFIG_DIR": "/sys/admin/coding-cli/claude", "CREMIND_PROFILE": "admin"}

    async def _body():
        return await terminals.create_terminal(
            "admin",
            cwd="/work",
            cols=120,
            rows=40,
            extra_env=env,
            argv=["/opt/cremind/bin/claude", "auth", "login"],
            title="Sign in to Claude Code",
        )

    info = _run(_body)

    assert spawner.shell_calls == []
    assert spawner.argv_calls == [{
        "argv": ["/opt/cremind/bin/claude", "auth", "login"],
        "working_dir": "/work",
        "cols": 120,
        "rows": 40,
        "extra_env": env,
    }]
    # The card and the terminal tab show what is running, not "powershell".
    assert info.shell == "claude"
    assert info.title == "Sign in to Claude Code"
    assert info.working_dir == "/work"
    assert info.profile == "admin"
    assert info.terminal_id.startswith("term-")
    assert terminals._terminal_registry[info.terminal_id] is info
    assert info.pump_task is not None
    # No subscribers yet: the WS attaches after the POST returns, and the reap
    # sweep must see the session as detached from the start.
    assert info.detached_since is not None


def test_argv_terminal_does_not_consume_a_shell_terminal_number(spawner):
    """A titled sign-in terminal must not burn a number, or the user's next
    "New terminal" jumps from Terminal 1 to Terminal 3."""

    async def _body():
        first = await terminals.create_terminal(
            "admin", cwd="/work", cols=80, rows=24, extra_env={},
            argv=["/usr/bin/codex", "login", "--device-auth"],
            title="Sign in to Codex",
        )
        second = await terminals.create_terminal(
            "admin", cwd="/work", cols=80, rows=24, extra_env={},
        )
        return first, second

    first, second = _run(_body)

    assert first.title == "Sign in to Codex"
    assert first.shell == "codex"
    assert second.title == "Terminal 1"


def test_an_empty_argv_is_refused_rather_than_becoming_a_shell(spawner):
    """A caller whose ``find_cli`` came back empty must hear about it, not get
    a bare prompt in a tab labelled "Sign in to Claude Code"."""

    async def _body():
        with pytest.raises(ValueError):
            await terminals.create_terminal(
                "admin", cwd="/work", cols=80, rows=24, extra_env={},
                argv=[], title="Sign in to Claude Code",
            )

    _run(_body)

    assert spawner.argv_calls == []
    assert spawner.shell_calls == []
    assert terminals._terminal_registry == {}


def test_shell_terminal_path_is_untouched(spawner):
    """argv=None keeps the original behaviour byte for byte."""

    async def _body():
        return await terminals.create_terminal(
            "admin", cwd="/work", cols=100, rows=30,
            extra_env={"CREMIND_PROFILE": "admin"},
        )

    info = _run(_body)

    assert spawner.argv_calls == []
    assert spawner.shell_calls == [{
        "working_dir": "/work",
        "cols": 100,
        "rows": 30,
        "extra_env": {"CREMIND_PROFILE": "admin"},
    }]
    assert info.shell == "bash"  # whatever the shell spawner reported
    assert info.title == "Terminal 1"


# ---------------------------------------------------------------------------
# The guards the extraction had to keep
# ---------------------------------------------------------------------------

def test_the_cap_counts_argv_terminals_too(spawner):
    """Sign-in terminals are terminals: they cannot be looped to fork-bomb the
    server, and they are counted per profile, not globally."""
    cap = terminals._MAX_TERMINALS_PER_PROFILE

    async def _body():
        for _ in range(cap):
            await terminals.create_terminal(
                "admin", cwd="/work", cols=80, rows=24, extra_env={},
                argv=["/usr/bin/codex", "login"], title="Sign in to Codex",
            )
        with pytest.raises(terminals.TerminalLimitReached) as excinfo:
            await terminals.create_terminal(
                "admin", cwd="/work", cols=80, rows=24, extra_env={},
                argv=["/usr/bin/codex", "login"], title="Sign in to Codex",
            )
        # A second profile is unaffected by the first one's terminals.
        other = await terminals.create_terminal(
            "bob", cwd="/work", cols=80, rows=24, extra_env={},
            argv=["/usr/bin/codex", "login"], title="Sign in to Codex",
        )
        return str(excinfo.value), other

    message, other = _run(_body)

    assert message == f"Too many open terminals (max {cap})."
    assert other.profile == "bob"
    assert len(terminals._terminal_registry) == cap + 1


def test_creating_a_terminal_still_reaps_a_long_detached_one(spawner):
    """The reap sweep lives on the create path; it has no timer of its own."""

    async def _body():
        stale = await terminals.create_terminal(
            "admin", cwd="/work", cols=80, rows=24, extra_env={},
        )
        stale.detached_since -= terminals._DETACHED_REAP_SECONDS + 1
        await terminals.create_terminal(
            "admin", cwd="/work", cols=80, rows=24, extra_env={},
            argv=["/usr/bin/codex", "login"], title="Sign in to Codex",
        )
        # _reap_stale only schedules the termination.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return stale

    stale = _run(_body)

    assert stale.process.terminated is True


# ---------------------------------------------------------------------------
# The route on top of it
# ---------------------------------------------------------------------------

def _create_route_handler():
    for route in terminals.get_terminal_routes():
        if getattr(route, "path", "") == "/api/terminals" and "POST" in (
            getattr(route, "methods", None) or ()
        ):
            return route.endpoint
    raise AssertionError("POST /api/terminals is gone")


def _req(body: Any, *, username: str = "admin", authed: bool = True):
    async def _json() -> Any:
        return body

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authed, username=username),
        json=_json,
    )


def _payload(response) -> Dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def test_post_terminals_keeps_its_request_and_response_shape(spawner, monkeypatch, tmp_path):
    """The route is now a thin wrapper, but its contract with TerminalSession.vue
    (cwd/cols/rows in, terminal_id/title/shell/working_dir/created_at out) must
    not have moved with the extraction."""
    monkeypatch.setattr(terminals, "build_system_env", lambda p: {"CREMIND_PROFILE": p})
    handler = _create_route_handler()
    cwd = str(tmp_path)

    async def _body():
        return await handler(_req({"cwd": cwd, "cols": 111, "rows": 33}))

    response = _run(_body)

    assert response.status_code == 201
    payload = _payload(response)
    assert set(payload) == {
        "terminal_id", "title", "shell", "working_dir", "created_at",
    }
    assert payload["title"] == "Terminal 1"
    assert payload["shell"] == "bash"
    assert payload["working_dir"] == cwd
    assert spawner.shell_calls == [{
        "working_dir": cwd,
        "cols": 111,
        "rows": 33,
        "extra_env": {"CREMIND_PROFILE": "admin"},
    }]


def test_post_terminals_still_answers_409_at_the_cap(spawner, monkeypatch, tmp_path):
    """``TerminalLimitReached`` has to come back out as the same 409 the UI
    already renders, not as a 500."""
    monkeypatch.setattr(terminals, "build_system_env", lambda p: {})
    handler = _create_route_handler()
    cap = terminals._MAX_TERMINALS_PER_PROFILE

    async def _body():
        for _ in range(cap):
            await terminals.create_terminal(
                "admin", cwd=str(tmp_path), cols=80, rows=24, extra_env={},
            )
        return await handler(_req({"cwd": str(tmp_path)}))

    response = _run(_body)

    assert response.status_code == 409
    assert _payload(response) == {
        "error": f"Too many open terminals (max {cap})."
    }


def test_post_terminals_reports_a_spawn_failure_as_500(spawner, monkeypatch, tmp_path):
    """A failed spawn is the caller's to render; create_terminal only logs."""
    async def _boom(*args, **kwargs):
        raise RuntimeError("pywinpty is required")

    monkeypatch.setattr(terminals, "build_system_env", lambda p: {})
    monkeypatch.setattr(terminals, "spawn_interactive_shell_pty", _boom)
    handler = _create_route_handler()

    async def _body():
        return await handler(_req({"cwd": str(tmp_path)}))

    response = _run(_body)

    assert response.status_code == 500
    assert "pywinpty is required" in _payload(response)["error"]
    assert terminals._terminal_registry == {}


def test_post_terminals_still_requires_auth(spawner):
    handler = _create_route_handler()

    async def _body():
        return await handler(_req({}, authed=False))

    response = _run(_body)

    assert response.status_code == 401
    assert spawner.shell_calls == []
