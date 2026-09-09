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

And the race that the sign-in flow turned from a curiosity into a bug: a login
binary can die before the browser has opened its WebSocket, because the pump
starts before the 201 is written. Its own error text - a bad CLI path, an
unwritable CLAUDE_CONFIG_DIR, a wrong-arch binary on the K8s venv PVC - is the
answer the user came for, so an exited terminal has to outlive itself long
enough to be read once, then be collected. That is pinned here too: it stays in
the registry, it does not occupy a cap slot, the reaper eventually pops it
without pretending to terminate a corpse, and a late attach is answered with
the scrollback and a 1000 close rather than the 1008 that used to greet it.

The PTY itself is faked: a real one would need a tty (and a login binary) on the
test box, and every assertion here is about the registration around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


class _EofReader:
    """A stdout reader already at EOF, with one line of output behind it.

    The counterpart to :class:`_ParkedReader`, and the whole point of the
    exit-before-attach tests: ``_pump_output`` runs its ``finally`` in the first
    cycle, exactly as it does for a login binary that dies on startup. The line
    stands in for the error text that is the only reason to keep such a
    terminal readable at all.
    """

    def __init__(self, text: bytes = b"claude: command not found\r\n") -> None:
        self._pending = text

    async def read(self, _n: int) -> bytes:
        data, self._pending = self._pending, b""
        return data


class _FakePty:
    """The subset of ``PtyProcess`` the registry and the pump touch."""

    def __init__(self, reader: Any = None) -> None:
        self.pid = 4242
        self.stdout = reader if reader is not None else _ParkedReader()
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
        # Swapped for ``_EofReader`` by the tests about an exited terminal; the
        # default parks the pump so everything else can assert on a live one.
        self.reader_factory: Callable[[], Any] = _ParkedReader

    def _new(self) -> _FakePty:
        proc = _FakePty(self.reader_factory())
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
        drop_env: Optional[Any] = None,
    ) -> _FakePty:
        self.argv_calls.append({
            "argv": argv,
            "working_dir": working_dir,
            "cols": cols,
            "rows": rows,
            "extra_env": extra_env,
            "drop_env": drop_env,
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
        # Nothing dropped by default: on a native install the user's own
        # browser opening is the desired behaviour.
        "drop_env": None,
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


def test_drop_env_reaches_the_spawner_verbatim(spawner):
    """The container sign-in path needs a variable *gone*, not overridden.

    ``extra_env`` cannot express that - no value of ``DISPLAY`` means "there is
    no display" - so the names travel separately, and this is the seam where
    they could quietly be lost."""

    async def _body():
        return await terminals.create_terminal(
            "admin", cwd="/work", cols=80, rows=24, extra_env={"CODEX_HOME": "/h"},
            argv=["/usr/bin/codex", "login", "--device-auth"],
            title="Sign in to Codex",
            drop_env=("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"),
        )

    _run(_body)

    assert spawner.argv_calls[0]["drop_env"] == (
        "DISPLAY", "WAYLAND_DISPLAY", "BROWSER",
    )
    # The overlay is untouched by the drop list; they are two separate answers.
    assert spawner.argv_calls[0]["extra_env"] == {"CODEX_HOME": "/h"}


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
# A login binary that dies before the browser attaches
# ---------------------------------------------------------------------------

async def _spawn_exited(spawner: _Spawner) -> terminals.TerminalInfo:
    """A sign-in terminal whose process is already gone on return.

    Awaiting the pump task is what makes this deterministic: the pump's
    ``finally`` is where the exit code, the linger stamp and the keep-or-pop
    decision all happen, and a test that raced it would be asserting on a
    half-dead terminal.
    """
    spawner.reader_factory = _EofReader
    info = await terminals.create_terminal(
        "admin", cwd="/work", cols=80, rows=24, extra_env={},
        argv=["/opt/claude", "auth", "login"], title="Sign in to Claude Code",
    )
    assert info.pump_task is not None
    await info.pump_task
    return info


def test_an_exited_terminal_nobody_saw_is_kept_readable(spawner):
    """The pump must not pop a terminal whose ``__closed`` went nowhere.

    ``create_terminal`` starts the pump before the 201 is written and the
    browser opens the socket a round trip later, so popping on exit is what
    turned a login binary that died instantly into a blank box and a toast. The
    scrollback survives with it, because the command's own error text is the
    entire reason to keep the corpse.
    """
    cap = terminals._MAX_TERMINALS_PER_PROFILE

    async def _body():
        info = await _spawn_exited(spawner)
        # And it holds no cap slot: it owns no process and no fd, so a handful
        # of failed sign-ins must not answer the next one with a 409 in place
        # of the error the user is trying to read.
        spawner.reader_factory = _ParkedReader
        for _ in range(cap):
            await terminals.create_terminal(
                "admin", cwd="/work", cols=80, rows=24, extra_env={},
            )
        return info

    info = _run(_body)

    assert info.exit_code == 0
    assert info.exited_at is not None
    assert terminals._terminal_registry[info.terminal_id] is info
    assert "".join(info.ring) == "claude: command not found\r\n"
    assert len(terminals._terminal_registry) == cap + 1


def test_the_reaper_pops_an_exited_terminal_rather_than_terminating_it(spawner):
    """Termination is the wrong verb for a corpse, and would leave it forever.

    ``_terminate`` on a dead process raises into its own ``except`` and never
    removes the entry, so the one case the linger window exists for would be
    the one case the sweep could not clean up.
    """

    async def _body():
        info = await _spawn_exited(spawner)
        info.exited_at -= terminals._EXITED_LINGER_SECONDS + 1
        terminals._reap_stale()
        return info

    info = _run(_body)

    assert info.terminal_id not in terminals._terminal_registry
    assert info.process.terminated is False
    assert info.process.killed is False


def test_a_late_websocket_reads_the_scrollback_and_a_clean_close(spawner, monkeypatch):
    """The user-visible fix: attaching after the exit answers, it does not reject.

    Driven through the real WebSocket route, because the bug was in the
    handshake - the registry lookup found nothing and closed with 1008, and the
    dialog rendered a toast instead of the sign-in command's own output. The
    token gate itself belongs to ``processes.py`` and is pinned there; here the
    subject is everything that happens after it, so the decoder is stubbed.

    The 1000 close is as load-bearing as the frames: the pump has finished, so
    nothing will ever put ``__closed`` on this queue, and a handler that fell
    through to its normal loop would hold the socket open forever.
    """
    monkeypatch.setattr(
        terminals,
        "_decode_ws_token",
        lambda token: {"profile": "admin"} if token == "ws-token" else None,
    )
    holder: Dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def _lifespan(_app) -> AsyncIterator[None]:
        # Inside the client's own loop: the terminal's lock and pump task have
        # to belong to the loop the WebSocket handler will run on.
        info = await _spawn_exited(spawner)
        holder["tid"] = info.terminal_id
        yield

    app = Starlette(routes=terminals.get_terminal_routes(), lifespan=_lifespan)
    with TestClient(app) as client:
        tid = holder["tid"]
        with client.websocket_connect(
            f"/api/terminals/{tid}/ws", subprotocols=["bearer", "ws-token"],
        ) as websocket:
            snapshot = websocket.receive_json()
            status = websocket.receive_json()
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_json()

    assert snapshot["type"] == "snapshot"
    assert [c["data"] for c in snapshot["chunks"]] == ["claude: command not found\r\n"]
    assert status["type"] == "status"
    assert status["data"]["status"] == "exited"
    assert status["data"]["exit_code"] == 0
    assert excinfo.value.code == 1000
    # Read once by the client that was waiting for it, so it retires now rather
    # than sitting out the rest of its linger window.
    assert holder["tid"] not in terminals._terminal_registry


class _GatedReader:
    """A stdout reader that holds its line until a gate opens, then EOFs.

    Lets a test place the process's whole death *inside* the WebSocket
    handshake, which is the one interleaving the linger window does not cover
    on its own.
    """

    def __init__(self, gate: asyncio.Event, text: bytes = b"claude: command not found\r\n"):
        self._gate = gate
        self._pending = text

    async def read(self, _n: int) -> bytes:
        await self._gate.wait()
        pending, self._pending = self._pending, b""
        return pending


class _FakeWebSocket:
    """The subset of ``WebSocket`` the handler touches, recording every frame.

    Its ``accept`` is the hook: the handshake is where the race lives, so that
    is where the test lets the process die.
    """

    def __init__(self, tid: str, on_accept=None) -> None:
        self.scope = {"subprotocols": ["bearer", "ws-token"]}
        self.path_params = {"tid": tid}
        self.sent: List[Dict[str, Any]] = []
        self.close_code: Optional[int] = None
        self._on_accept = on_accept

    async def accept(self, subprotocol: Optional[str] = None) -> None:
        if self._on_accept is not None:
            await self._on_accept()

    async def send_json(self, message: Dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    async def receive_text(self) -> str:  # pragma: no cover - never reached here
        raise AssertionError("the exited path must not read from the client")


def _ws_endpoint():
    for route in terminals.get_terminal_routes():
        if getattr(route, "path", "") == "/api/terminals/{tid}/ws":
            return route.endpoint
    raise AssertionError("the terminal WebSocket route is missing")


def test_a_process_that_dies_during_the_handshake_still_delivers_its_output(
    spawner, monkeypatch,
):
    """The output must survive dying *between* the snapshot and the exit check.

    ``_subscribe`` takes the scrollback and registers the queue atomically, so a
    chunk the pump writes after that moment exists ONLY in this subscriber's
    queue - it is not in the snapshot already taken. The handler then yields
    three times (accept, and two sends) before it asks whether the process has
    exited, and the pump can finish inside that window: it appends the line to
    the ring, fans it into the queue and sets the exit code.

    Returning at that point without draining the queue threw the line away and
    then popped the entry that still held it, so a login binary that failed in
    the first millisecond produced a blank terminal under the words "it did not
    sign in - its own output above says why". There was no output above.
    """
    monkeypatch.setattr(
        terminals, "_decode_ws_token", lambda token: {"profile": "admin"},
    )

    async def _run_it() -> _FakeWebSocket:
        gate = asyncio.Event()
        spawner.reader_factory = lambda: _GatedReader(gate)
        info = await terminals.create_terminal(
            "admin", cwd="/work", cols=80, rows=24, extra_env={},
            argv=["/opt/claude", "auth", "login"], title="Sign in to Claude Code",
        )
        # Still alive when the handler subscribes: the snapshot it takes is
        # empty, which is what makes the queue the only copy of the line.
        assert info.exit_code is None
        assert not info.ring

        async def _die_during_accept() -> None:
            gate.set()
            assert info.pump_task is not None
            await info.pump_task

        websocket = _FakeWebSocket(info.terminal_id, on_accept=_die_during_accept)
        await _ws_endpoint()(websocket)
        return websocket

    websocket = asyncio.run(_run_it())

    kinds = [m["type"] for m in websocket.sent]
    assert kinds[0] == "snapshot"
    assert websocket.sent[0]["chunks"] == []
    # Forwarded from the queue rather than lost with it.
    streamed = "".join(
        m.get("data", "") for m in websocket.sent if m["type"] in ("stdout", "stderr")
    )
    assert streamed == "claude: command not found\r\n"
    status = websocket.sent[-1]
    assert status["type"] == "status"
    assert status["data"]["status"] == "exited"
    assert websocket.close_code == 1000


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


def _req(
    body: Any,
    *,
    username: str = "admin",
    authed: bool = True,
    path_params: Optional[Dict[str, str]] = None,
):
    async def _json() -> Any:
        return body

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authed, username=username),
        path_params=path_params or {},
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


def _close_route_handler():
    for route in terminals.get_terminal_routes():
        if getattr(route, "path", "") == "/api/terminals/{tid}/close":
            return route.endpoint
    raise AssertionError("POST /api/terminals/{tid}/close is gone")


def test_closing_an_exited_terminal_retires_it(spawner):
    """The sign-in dialog closes on dismiss, and that is the user saying they
    are done reading - so close is what retires a corpse early instead of
    waiting out the linger window. Idempotent by way of the 404 an unknown id
    already gets: a second dismiss must not read as a failure the UI reports."""
    handler = _close_route_handler()

    async def _body():
        info = await _spawn_exited(spawner)
        first = await handler(_req({}, path_params={"tid": info.terminal_id}))
        second = await handler(_req({}, path_params={"tid": info.terminal_id}))
        return info, first, second

    info, first, second = _run(_body)

    assert first.status_code == 200
    assert _payload(first) == {"ok": True}
    assert info.terminal_id not in terminals._terminal_registry
    assert second.status_code == 404


def test_post_terminals_still_requires_auth(spawner):
    handler = _create_route_handler()

    async def _body():
        return await handler(_req({}, authed=False))

    response = _run(_body)

    assert response.status_code == 401
    assert spawner.shell_calls == []
