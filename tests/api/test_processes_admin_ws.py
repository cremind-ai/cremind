"""The admin may WATCH a room-mate's process; nobody but the owner may drive it.

A group room's right-hand panel shows the admin every member agent's terminal,
the same reach the admin already has over every agent's reasoning trace. That is
a read, and the socket has to stay one: an earlier version acted AS the owner for
the rest of the connection so the ownership re-check inside ``exec_shell`` would
pass, which turned "watch a member's terminal" into a shell running as that
member — arbitrary commands under their environment, and a process the same
handler is not allowed to stop, stopped by typing ``exit``.

So two rules are pinned here. Reading is relaxed, and only as far as its
justification reaches: ``admin``, for an owner it shares a room with. Driving —
stdin, resize, stop — is not relaxed at all, and a refused frame says so on the
socket instead of vanishing into a gate that reports nothing back.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from starlette.routing import Route, WebSocketRoute  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import app.groups.index as group_index  # noqa: E402
from app.api import processes as processes_api  # noqa: E402

PID = "proc-1"
ROOM = "g-room"


class _WS:
    """Minimal WebSocket double: scripted client frames, then a disconnect."""

    def __init__(self, token: str, pid: str = PID, inbox=None):
        self.scope = {"subprotocols": ["bearer", token]}
        self.path_params = {"pid": pid}
        self.accepted_subprotocol: str | None = None
        self.close_code: int | None = None
        self.sent: list[dict] = []
        self._inbox = list(inbox or [])

    async def accept(self, subprotocol=None):
        self.accepted_subprotocol = subprotocol

    async def close(self, code=1000):
        self.close_code = code

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_text(self):
        if self._inbox:
            return self._inbox.pop(0)
        raise WebSocketDisconnect(1000)

    # ── assertions read the transcript through these ──

    def frames(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]

    @property
    def status(self) -> dict:
        return self.frames("status")[0]["data"]


def _handler():
    for route in processes_api.get_process_routes():
        if isinstance(route, WebSocketRoute) and route.path.endswith("/ws"):
            return route.endpoint
    raise AssertionError("process ws route not registered")


def _route(path: str, method: str):
    for route in processes_api.get_process_routes():
        if isinstance(route, Route) and route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _setup(monkeypatch, owner="member"):
    """Register one running process owned by *owner* and capture its stdin."""
    info = SimpleNamespace(
        profile=owner, command="npm run dev", working_dir="/w",
        log_dir="/w/logs", is_pty=True, created_at=0.0,
    )
    monkeypatch.setattr(processes_api, "_process_registry", {PID: info})
    monkeypatch.setattr(processes_api, "process_status", lambda _i: ("running", None))

    subscribed: list[str] = []

    async def fake_subscribe(pid):
        subscribed.append(pid)
        return asyncio.Queue(), [("stdout", "hello")]

    async def fake_unsubscribe(pid, queue):
        return None

    stdin_calls: list[dict] = []

    async def fake_stdin(pid, **kwargs):
        stdin_calls.append({"pid": pid, **kwargs})
        return {"ok": True}

    resize_calls: list[dict] = []

    async def fake_resize(pid, cols, rows, *, profile):
        resize_calls.append({"pid": pid, "cols": cols, "rows": rows, "profile": profile})
        return {"ok": True}

    monkeypatch.setattr(processes_api, "subscribe", fake_subscribe)
    monkeypatch.setattr(processes_api, "unsubscribe", fake_unsubscribe)
    monkeypatch.setattr(processes_api, "write_stdin_to_process", fake_stdin)
    monkeypatch.setattr(processes_api, "resize_pty", fake_resize)
    return subscribed, stdin_calls, resize_calls


def _as(monkeypatch, profile):
    monkeypatch.setattr(
        processes_api, "_decode_ws_token", lambda _t: {"sub": profile},
    )


def _rooms(monkeypatch, membership: dict[str, set[str]]):
    """Stand in for the live ``GroupIndex`` with a fixed profile → rooms map.

    Only ``groups_for_profile`` is provided, so the real intersection in
    ``_shares_a_room`` is what the tests exercise; patching that helper directly
    would leave the scoping rule itself uncovered.
    """
    fake = SimpleNamespace(
        groups_for_profile=lambda p: set(membership.get(p, ())),
    )
    monkeypatch.setattr(group_index, "get_group_index", lambda: fake)


def _share_a_room(monkeypatch, *profiles: str):
    _rooms(monkeypatch, {p: {ROOM} for p in profiles})


_STDIN = json.dumps({"type": "stdin", "data": "ls\n"})
_RESIZE = json.dumps({"type": "resize", "cols": 120, "rows": 40})
_STOP = json.dumps({"type": "stop"})


def test_the_admin_attaches_to_a_room_mates_process_and_reads_it(monkeypatch):
    subscribed, _stdin, _resize = _setup(monkeypatch, owner="member")
    _as(monkeypatch, "admin")
    _share_a_room(monkeypatch, "admin", "member")
    ws = _WS("t")

    asyncio.run(_handler()(ws))

    assert ws.close_code is None
    assert ws.accepted_subprotocol == "bearer"
    # Output really does stream: the panel is the whole point of the relaxation.
    assert subscribed == [PID]
    assert ws.frames("snapshot")[0]["chunks"] == [{"type": "stdout", "data": "hello"}]
    # And the socket says up front that it is a view, not a terminal.
    assert ws.status["read_only"] is True


def test_a_watchers_stdin_resize_and_stop_are_all_refused(monkeypatch):
    _subscribed, stdin_calls, resize_calls = _setup(monkeypatch, owner="member")
    _as(monkeypatch, "admin")
    _share_a_room(monkeypatch, "admin", "member")
    ws = _WS("t", inbox=[_STDIN, _RESIZE, _STOP])

    asyncio.run(_handler()(ws))

    # Nothing reached exec_shell — and in particular nothing reached it wearing
    # the owner's name, which is how this socket used to smuggle commands in.
    assert stdin_calls == []
    assert resize_calls == []
    # Three frames in, three refusals back: a swallowed keystroke would leave
    # the viewer typing into what looks like a live shell.
    refusals = ws.frames("error")
    assert len(refusals) == 3
    assert all(r["error"] == "Forbidden" for r in refusals)
    assert "read-only" in refusals[0]["message"]
    # Refusing input is not a reason to tear down the view.
    assert ws.close_code is None


def test_the_admin_is_closed_out_of_a_process_from_no_shared_room(monkeypatch):
    subscribed, stdin_calls, _resize = _setup(monkeypatch, owner="stranger")
    _as(monkeypatch, "admin")
    _rooms(monkeypatch, {"admin": {ROOM}, "stranger": {"g-other"}})
    ws = _WS("t", inbox=[_STDIN])

    asyncio.run(_handler()(ws))

    # The panel justifies seeing the agents in one's own rooms, nothing wider.
    assert ws.close_code == 1008
    assert subscribed == []
    assert stdin_calls == []


def test_an_unloaded_group_index_falls_back_to_owner_only(monkeypatch):
    """Boot order must not be able to widen the rule — no index, no relaxation."""
    subscribed, _stdin, _resize = _setup(monkeypatch, owner="member")
    _as(monkeypatch, "admin")

    def _boom():
        raise RuntimeError("index not loaded")

    monkeypatch.setattr(group_index, "get_group_index", _boom)
    ws = _WS("t")

    asyncio.run(_handler()(ws))

    assert ws.close_code == 1008
    assert subscribed == []


def test_another_member_is_still_closed(monkeypatch):
    subscribed, stdin_calls, _resize = _setup(monkeypatch, owner="member")
    _as(monkeypatch, "intruder")
    _share_a_room(monkeypatch, "intruder", "member")
    ws = _WS("t", inbox=[_STDIN])

    asyncio.run(_handler()(ws))

    # Sharing a room buys an ordinary member nothing: the relaxation is the
    # admin's alone.
    assert ws.close_code == 1008
    assert ws.accepted_subprotocol is None
    # The refusal precedes the subscription, so no output ever streamed.
    assert subscribed == []
    assert stdin_calls == []


def test_the_owner_is_unaffected(monkeypatch):
    _subscribed, stdin_calls, resize_calls = _setup(monkeypatch, owner="member")
    _as(monkeypatch, "member")
    _share_a_room(monkeypatch, "admin", "member")
    ws = _WS("t", inbox=[_STDIN, _RESIZE])

    asyncio.run(_handler()(ws))

    assert ws.close_code is None
    assert ws.status["read_only"] is False
    assert stdin_calls[0]["profile"] == "member"
    assert resize_calls[0]["profile"] == "member"
    assert ws.frames("error") == []


def test_an_untagged_process_still_takes_input(monkeypatch):
    """A process with no owner recorded is nobody's to lose — matching
    ``exec_shell._require_process``, which only enforces when both names exist.
    """
    _subscribed, stdin_calls, _resize = _setup(monkeypatch, owner="")
    _as(monkeypatch, "member")
    ws = _WS("t", inbox=[_STDIN])

    asyncio.run(_handler()(ws))

    assert stdin_calls[0]["profile"] == "member"


def test_get_lets_the_admin_read_only_a_room_mates_process(monkeypatch):
    _setup(monkeypatch, owner="member")
    _share_a_room(monkeypatch, "admin", "member")

    def _req(username):
        return SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True, username=username),
            path_params={"pid": PID},
        )

    handler = _route("/api/processes/{pid}", "GET")

    assert asyncio.run(handler(_req("admin"))).status_code == 200
    assert asyncio.run(handler(_req("member"))).status_code == 200
    assert asyncio.run(handler(_req("intruder"))).status_code == 403

    _rooms(monkeypatch, {"admin": {"g-elsewhere"}, "member": {ROOM}})
    assert asyncio.run(handler(_req("admin"))).status_code == 403


def test_the_rest_routes_never_act_as_the_owner(monkeypatch):
    """stop / stdin / resize over HTTP carry the caller's own name.

    They always did — the WebSocket was the hole — so this is the regression
    fence around the claim that only the socket needed fixing.
    """
    _setup(monkeypatch, owner="member")
    _share_a_room(monkeypatch, "admin", "member")
    seen: list[str] = []

    async def fake_stop(pid, *, profile):
        seen.append(profile)
        return {"error": "Forbidden"}

    async def fake_stdin(pid, **kwargs):
        seen.append(kwargs["profile"])
        return {"error": "Forbidden"}

    async def fake_resize(pid, cols, rows, *, profile):
        seen.append(profile)
        return {"ok": False, "error": "Forbidden"}

    monkeypatch.setattr(processes_api, "stop_process", fake_stop)
    monkeypatch.setattr(processes_api, "write_stdin_to_process", fake_stdin)
    monkeypatch.setattr(processes_api, "resize_pty", fake_resize)

    class _Req:
        def __init__(self, body):
            self.user = SimpleNamespace(is_authenticated=True, username="admin")
            self.path_params = {"pid": PID}
            self._body = body

        async def json(self):
            return self._body

    stop = _route("/api/processes/{pid}/stop", "POST")
    stdin = _route("/api/processes/{pid}/stdin", "POST")
    resize = _route("/api/processes/{pid}/resize", "POST")

    assert asyncio.run(stop(_Req({}))).status_code == 403
    assert asyncio.run(stdin(_Req({"input_text": "ls"}))).status_code == 403
    assert asyncio.run(resize(_Req({"cols": 80, "rows": 24}))).status_code == 403
    assert seen == ["admin", "admin", "admin"]
