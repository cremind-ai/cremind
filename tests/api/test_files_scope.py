"""Per-request conversation scoping in ``app.api.files``.

Every file route that accepts a ``conversation_id`` uses it to widen the path
allowlist to whatever directory that conversation was switched into. Two things
were wrong with the raw id.

It was unchecked: any authenticated profile could name someone else's
conversation and read, upload into, or delete through that conversation's custom
cwd. Ownership is now required — with an admin exception scoped to the *reads*,
because what the group room's right-hand panel does with a member's seat is
render its file tree. The exception used to cover the whole helper, so it also
let the admin delete under another profile's cwd and POST ``/api/files/cwd``,
which repoints that member's running agent mid-turn at a directory of the
admin's choosing. Hence the ``write`` flag, and hence the half of this file that
walks every mutating route.

And it was the wrong key: the override is stored under the conversation's
``context_id``, which for a group-chat seat is ``group:<gid>:<profile>``, not the
row id the client sends. Looked up by the row id it simply wasn't there, so the
tree never followed the seat's agent.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.api import files as files_api  # noqa: E402
from app.utils.context_storage import (  # noqa: E402
    clear_context,
    get_context,
    set_context,
)
from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY  # noqa: E402

SEAT_CONTEXT = "group:g-files:member"
SEAT_ROW = "conv-seat-files"


class _Req:
    def __init__(self, username="member", query=None, body=None, form=None):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.query_params = query or {}
        self._body = body or {}
        self._form = form

    async def json(self):
        return self._body

    async def form(self):
        return self._form

    async def is_disconnected(self):
        return True


class _Form:
    """The slice of Starlette's ``FormData`` the upload route touches."""

    def __init__(self, fields: dict, files: list):
        self._fields = fields
        self._files = files

    def get(self, key, default=None):
        return self._fields.get(key, default)

    def multi_items(self):
        return [*self._fields.items(), *((f.filename, f) for f in self._files)]


class _Upload:
    """One multipart file part: a name and one chunk of bytes."""

    def __init__(self, filename: str, data: bytes = b"x"):
        self.filename = filename
        self._chunks = [data]

    async def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeStorage:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []

    async def get_conversation(self, conversation_id):
        return self.rows.get(conversation_id)

    async def update_conversation(self, conversation_id, **kwargs):
        self.updates.append((conversation_id, kwargs))


class _RecordingBus:
    def __init__(self):
        self.published: list[tuple[str, str, dict]] = []

    async def publish(self, channel, event, payload):
        self.published.append((channel, event, payload))


def _body(resp) -> dict:
    return json.loads(resp.body)


def _setup(tmp_path, monkeypatch) -> _FakeStorage:
    """Pin both static allowlist bases inside tmp so the seat cwd is outside them."""
    system_dir = tmp_path / "system"
    user_dir = tmp_path / "userwd"
    system_dir.mkdir()
    user_dir.mkdir()
    monkeypatch.setattr(files_api.BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir))
    monkeypatch.setattr(files_api, "get_user_working_directory", lambda: str(user_dir))

    storage = _FakeStorage({SEAT_ROW: {
        "id": SEAT_ROW, "context_id": SEAT_CONTEXT, "profile": "member",
    }})
    import app.events.runner as runner
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: storage)
    return storage


def _seat_cwd(tmp_path) -> str:
    """A directory outside every static base, held as the seat's live override."""
    target = tmp_path / "outside"
    target.mkdir(exist_ok=True)
    resolved = os.path.realpath(str(target))
    (target / "notes.txt").write_text("x")
    set_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY, resolved)
    return resolved


def _list(username, path, conversation_id=None):
    query = {"path": path}
    if conversation_id:
        query["conversation_id"] = conversation_id
    return asyncio.run(files_api._list_directory(_Req(username=username, query=query)))


def test_listing_widens_from_the_override_stored_under_the_seat_context(
    tmp_path, monkeypatch,
):
    _setup(tmp_path, monkeypatch)
    target = _seat_cwd(tmp_path)
    try:
        # Control: unattributed, the seat's cwd is outside every allowed base.
        assert _list("member", target).status_code == 403

        resp = _list("member", target, SEAT_ROW)

        assert resp.status_code == 200
        assert [e["name"] for e in _body(resp)["entries"]] == ["notes.txt"]
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_a_non_owner_member_may_not_borrow_the_scope(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    target = _seat_cwd(tmp_path)
    try:
        resp = _list("other", target, SEAT_ROW)

        assert resp.status_code == 403
        assert _body(resp)["error"] == "Forbidden"
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_the_admin_may_read_a_member_seats_tree(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    target = _seat_cwd(tmp_path)
    try:
        resp = _list("admin", target, SEAT_ROW)

        assert resp.status_code == 200
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_an_unknown_conversation_falls_back_to_the_static_bases(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    target = _seat_cwd(tmp_path)
    try:
        assert _list("member", target, "conv-does-not-exist").status_code == 403
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_mkdir_is_scoped_and_owner_checked(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    target = _seat_cwd(tmp_path)
    body = {"path": os.path.join(target, "sub"), "conversation_id": SEAT_ROW}
    try:
        denied = asyncio.run(files_api._mkdir(_Req(username="other", body=body)))
        assert denied.status_code == 403
        assert _body(denied)["error"] == "Forbidden"

        resp = asyncio.run(files_api._mkdir(_Req(username="member", body=body)))
        assert resp.status_code == 200
        assert os.path.isdir(os.path.join(target, "sub"))
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_set_cwd_keys_memory_by_context_and_persists_on_the_row(tmp_path, monkeypatch):
    storage = _setup(tmp_path, monkeypatch)
    bus = _RecordingBus()
    monkeypatch.setattr(files_api, "get_event_stream_bus", lambda: bus)
    target = tmp_path / "chosen"
    target.mkdir()
    resolved = os.path.realpath(str(target))

    try:
        resp = asyncio.run(files_api._set_cwd(_Req(
            username="member",
            body={"conversation_id": SEAT_ROW, "path": str(target)},
        )))

        assert resp.status_code == 200
        assert _body(resp)["working_directory"] == resolved
        # In-memory under the context the agent reads; DB + bus on the row.
        assert get_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY) == resolved
        assert get_context(SEAT_ROW, WORKING_DIR_OVERRIDE_KEY) is None
        assert storage.updates == [(SEAT_ROW, {"working_directory": resolved})]
        assert bus.published == [(SEAT_ROW, "cwd", {"working_directory": resolved})]
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_set_cwd_refuses_a_conversation_the_caller_does_not_own(tmp_path, monkeypatch):
    storage = _setup(tmp_path, monkeypatch)
    bus = _RecordingBus()
    monkeypatch.setattr(files_api, "get_event_stream_bus", lambda: bus)
    target = tmp_path / "chosen"
    target.mkdir()

    resp = asyncio.run(files_api._set_cwd(_Req(
        username="other", body={"conversation_id": SEAT_ROW, "path": str(target)},
    )))

    assert resp.status_code == 403
    assert storage.updates == []
    assert bus.published == []


def test_set_cwd_on_an_unknown_conversation_is_404(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    target = tmp_path / "chosen"
    target.mkdir()

    resp = asyncio.run(files_api._set_cwd(_Req(
        username="member", body={"conversation_id": "nope", "path": str(target)},
    )))

    assert resp.status_code == 404


# ── the admin bypass is a read, and only a read ──────────────────────────────


def _write_calls(target: str, elsewhere: str) -> dict:
    """Every mutating file route, each already aimed at the seat's own cwd.

    Collected in one place so a route added to ``files.py`` and not listed here
    reads as an omission, rather than slipping in as an untested bypass — which
    is the same reason ``_conversation_scope`` makes ``write`` mandatory.
    """
    return {
        "upload": lambda who: asyncio.run(files_api._upload_files(_Req(
            username=who,
            form=_Form(
                {"path": target, "conversation_id": SEAT_ROW},
                [_Upload("dropped.txt")],
            ),
        ))),
        "delete": lambda who: asyncio.run(files_api._delete_entry(_Req(
            username=who,
            body={"path": os.path.join(target, "doomed.txt"),
                  "conversation_id": SEAT_ROW},
        ))),
        "move": lambda who: asyncio.run(files_api._move_entry(_Req(
            username=who,
            body={"src": os.path.join(target, "notes.txt"),
                  "dest": os.path.join(target, "moved.txt"),
                  "conversation_id": SEAT_ROW},
        ))),
        "mkdir": lambda who: asyncio.run(files_api._mkdir(_Req(
            username=who,
            body={"path": os.path.join(target, "sub"),
                  "conversation_id": SEAT_ROW},
        ))),
        "set_cwd": lambda who: asyncio.run(files_api._set_cwd(_Req(
            username=who,
            body={"conversation_id": SEAT_ROW, "path": elsewhere},
        ))),
    }


def _writable_seat(tmp_path, monkeypatch):
    """A seat cwd holding the two files the write routes act on, plus a bus."""
    storage = _setup(tmp_path, monkeypatch)
    bus = _RecordingBus()
    monkeypatch.setattr(files_api, "get_event_stream_bus", lambda: bus)
    target = _seat_cwd(tmp_path)
    # Real, so a 403 can never be an existence check wearing a disguise.
    open(os.path.join(target, "doomed.txt"), "w").close()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    return storage, bus, target, os.path.realpath(str(elsewhere))


def test_no_mutating_route_admits_a_non_owner_admin(tmp_path, monkeypatch):
    storage, bus, target, elsewhere = _writable_seat(tmp_path, monkeypatch)
    try:
        for name, call in _write_calls(target, elsewhere).items():
            resp = call("admin")
            assert resp.status_code == 403, name
            assert _body(resp)["error"] == "Forbidden", name

        # Nothing moved, and — the sharpest edge of the five — the member's
        # running agent is still pointed where the member left it, not at the
        # directory the admin named.
        assert sorted(os.listdir(target)) == ["doomed.txt", "notes.txt"]
        assert get_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY) == target
        assert storage.updates == []
        assert bus.published == []
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_the_owner_still_writes_through_the_seat_scope(tmp_path, monkeypatch):
    storage, bus, target, elsewhere = _writable_seat(tmp_path, monkeypatch)
    calls = _write_calls(target, elsewhere)
    try:
        # Ordered so each route finds the tree the previous one left behind.
        assert calls["mkdir"]("member").status_code == 200
        assert calls["upload"]("member").status_code == 200
        assert calls["move"]("member").status_code == 200
        assert calls["delete"]("member").status_code == 200
        assert calls["set_cwd"]("member").status_code == 200

        assert sorted(os.listdir(target)) == ["dropped.txt", "moved.txt", "sub"]
        assert get_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY) == elsewhere
        assert storage.updates == [(SEAT_ROW, {"working_directory": elsewhere})]
        assert bus.published == [(SEAT_ROW, "cwd", {"working_directory": elsewhere})]
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_the_admin_may_open_and_watch_a_member_seats_tree(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    target = _seat_cwd(tmp_path)

    class _StubObserver:
        """Watchdog's Observer, minus the thread — the gate is what's on test."""

        def schedule(self, *_a, **_kw):
            return None

        def start(self):
            return None

        def stop(self):
            return None

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(files_api, "Observer", _StubObserver)

    def _open(who):
        return asyncio.run(files_api._serve_file_by_path(_Req(
            username=who,
            query={"path": os.path.join(target, "notes.txt"),
                   "conversation_id": SEAT_ROW},
        )))

    def _watch(who):
        return asyncio.run(files_api._watch_directory(_Req(
            username=who, query={"path": target, "conversation_id": SEAT_ROW},
        )))

    try:
        assert _open("admin").status_code == 200
        assert _watch("admin").status_code == 200
        # Still the admin's alone: another member gains nothing by naming the id.
        assert _open("other").status_code == 403
        assert _watch("other").status_code == 403
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)
