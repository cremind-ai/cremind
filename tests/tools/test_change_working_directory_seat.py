"""cwd plumbing when ``context_id`` is not a conversation id.

A group-chat seat is a conversation whose ``context_id`` is the room address
``group:<gid>:<profile>``. The agent loop hands that string to every tool as
``_context_id``, so ``change_working_directory`` used it for all three writes —
and two of them silently went nowhere: ``update_conversation("group:...")``
matched no row (the seat's cwd was never saved) and the ``cwd`` frame was
published on a bus channel no client subscribes to (no file tree ever moved).

These pin the split: the in-memory override stays under ``context_id`` because
that is the only key the agent reads, while the durable column and the event
channel are addressed by the resolved conversation ROW id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from app.tools.builtin import change_working_directory as cwd_tool  # noqa: E402
from app.utils.context_storage import clear_context, get_context  # noqa: E402
from app.utils.working_directory import (  # noqa: E402
    WORKING_DIR_OVERRIDE_KEY,
    hydrate_working_directory,
)

SEAT_CONTEXT = "group:g-cwd:member"
SEAT_ROW = "conv-seat-cwd"
PLAIN_ID = "conv-plain-cwd"


class _FakeStorage:
    """Just enough conversation storage for ``resolve_cwd_scope``."""

    def __init__(self, rows: dict[str, dict]):
        self.rows = rows
        self.updates: list[tuple[str, dict]] = []

    async def get_conversation_by_context(self, profile, context_id):
        for row in self.rows.values():
            if row.get("profile") == profile and row.get("context_id") == context_id:
                return row
        return None

    async def get_conversation(self, conversation_id):
        return self.rows.get(conversation_id)

    async def update_conversation(self, conversation_id, **kwargs):
        self.updates.append((conversation_id, kwargs))


class _RecordingBus:
    def __init__(self):
        self.published: list[tuple[str, str, dict]] = []

    async def publish(self, channel, event, payload):
        self.published.append((channel, event, payload))


def _switch(tmp_path, monkeypatch, storage, *, context_id, profile, target):
    """Run the tool's ``target='custom'`` branch and return the recording bus."""
    bus = _RecordingBus()
    monkeypatch.setattr(cwd_tool, "get_user_working_directory", lambda: str(tmp_path))
    monkeypatch.setattr(cwd_tool, "get_event_stream_bus", lambda: bus)
    import app.events.runner as runner
    monkeypatch.setattr(runner, "get_conversation_storage", lambda: storage)

    res = asyncio.run(cwd_tool.ChangeWorkingDirectoryTool().run({
        "target": "custom",
        "path": str(target),
        "_context_id": context_id,
        "_profile": profile,
    }))
    assert res.structured_content["current"] == str(Path(target).resolve())
    return bus


def test_a_seat_persists_and_publishes_on_its_conversation_row(tmp_path, monkeypatch):
    target = tmp_path / "work"
    target.mkdir()
    expected = str(Path(target).resolve())
    storage = _FakeStorage({SEAT_ROW: {
        "id": SEAT_ROW, "context_id": SEAT_CONTEXT, "profile": "member",
    }})

    try:
        bus = _switch(
            tmp_path, monkeypatch, storage,
            context_id=SEAT_CONTEXT, profile="member", target=target,
        )

        assert storage.updates == [(SEAT_ROW, {"working_directory": expected})]
        assert bus.published == [(SEAT_ROW, "cwd", {"working_directory": expected})]
        # The agent reads the override under the context it was called with —
        # moving that key to the row id would hide the switch from itself.
        assert get_context(SEAT_CONTEXT, cwd_tool.OVERRIDE_KEY) == expected
        assert get_context(SEAT_ROW, cwd_tool.OVERRIDE_KEY) is None
    finally:
        clear_context(SEAT_CONTEXT, cwd_tool.OVERRIDE_KEY)


def test_an_ordinary_conversation_is_unchanged(tmp_path, monkeypatch):
    """context_id == conversation_id, so all three writes share one id."""
    target = tmp_path / "plain"
    target.mkdir()
    expected = str(Path(target).resolve())
    storage = _FakeStorage({PLAIN_ID: {
        "id": PLAIN_ID, "context_id": PLAIN_ID, "profile": "p1",
    }})

    try:
        bus = _switch(
            tmp_path, monkeypatch, storage,
            context_id=PLAIN_ID, profile="p1", target=target,
        )

        assert storage.updates == [(PLAIN_ID, {"working_directory": expected})]
        assert bus.published == [(PLAIN_ID, "cwd", {"working_directory": expected})]
        assert get_context(PLAIN_ID, cwd_tool.OVERRIDE_KEY) == expected
    finally:
        clear_context(PLAIN_ID, cwd_tool.OVERRIDE_KEY)


def test_an_unresolvable_context_falls_back_to_itself(tmp_path, monkeypatch):
    """No row (a conversation created for this turn only) must not lose the switch."""
    target = tmp_path / "orphan"
    target.mkdir()
    expected = str(Path(target).resolve())
    storage = _FakeStorage({})

    try:
        bus = _switch(
            tmp_path, monkeypatch, storage,
            context_id="ctx-unknown", profile="p1", target=target,
        )

        assert storage.updates == [("ctx-unknown", {"working_directory": expected})]
        assert bus.published[0][0] == "ctx-unknown"
    finally:
        clear_context("ctx-unknown", cwd_tool.OVERRIDE_KEY)


def test_hydrate_restores_a_seat_cwd_under_its_context_key(tmp_path):
    """After a restart the persisted seat cwd must land where the agent looks."""
    target = tmp_path / "restored"
    target.mkdir()
    storage = _FakeStorage({SEAT_ROW: {
        "id": SEAT_ROW, "context_id": SEAT_CONTEXT, "profile": "member",
        "working_directory": str(target),
    }})

    try:
        got = asyncio.run(hydrate_working_directory(
            SEAT_ROW, storage, context_key=SEAT_CONTEXT,
        ))

        assert got == str(target)
        assert get_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY) == str(target)
        assert get_context(SEAT_ROW, WORKING_DIR_OVERRIDE_KEY) is None
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_hydrate_clears_a_stale_path_on_the_row_not_the_context(tmp_path):
    """The row id addresses the DB even while the context key addresses memory."""
    gone = tmp_path / "deleted"
    storage = _FakeStorage({SEAT_ROW: {
        "id": SEAT_ROW, "context_id": SEAT_CONTEXT, "profile": "member",
        "working_directory": str(gone),
    }})

    try:
        asyncio.run(hydrate_working_directory(
            SEAT_ROW, storage, context_key=SEAT_CONTEXT,
        ))

        assert storage.updates == [(SEAT_ROW, {"working_directory": None})]
        assert get_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY) is None
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)


def test_hydrate_without_a_context_key_keeps_the_conversation_id(tmp_path):
    target = tmp_path / "ordinary"
    target.mkdir()
    storage = _FakeStorage({PLAIN_ID: {
        "id": PLAIN_ID, "context_id": PLAIN_ID, "profile": "p1",
        "working_directory": str(target),
    }})

    try:
        got = asyncio.run(hydrate_working_directory(PLAIN_ID, storage))

        assert got == str(target)
        assert get_context(PLAIN_ID, WORKING_DIR_OVERRIDE_KEY) == str(target)
    finally:
        clear_context(PLAIN_ID, WORKING_DIR_OVERRIDE_KEY)


def test_resolve_cwd_scope_survives_a_storage_without_lookups():
    """The recovery path passes narrow fakes; an AttributeError there would
    abort a cwd switch mid-tool-call."""
    from app.utils.working_directory import resolve_cwd_scope

    class _Narrow:
        async def update_conversation(self, conversation_id, **kwargs):
            pass

    row_id, context_key = asyncio.run(
        resolve_cwd_scope(_Narrow(), context_id="ctx-x", profile="p1")
    )
    assert (row_id, context_key) == ("ctx-x", "ctx-x")
    assert asyncio.run(resolve_cwd_scope(None)) == ("", "")


def test_resolve_cwd_scope_maps_a_row_id_to_its_context_key():
    from app.utils.working_directory import resolve_cwd_scope

    storage = _FakeStorage({SEAT_ROW: {
        "id": SEAT_ROW, "context_id": SEAT_CONTEXT, "profile": "member",
    }})
    assert asyncio.run(
        resolve_cwd_scope(storage, conversation_id=SEAT_ROW)
    ) == (SEAT_ROW, SEAT_CONTEXT)
    # An id matching no row stands in for both halves.
    assert asyncio.run(
        resolve_cwd_scope(storage, conversation_id="nope")
    ) == ("nope", "nope")


def test_switch_conversation_cwd_addresses_the_seat_row(tmp_path, monkeypatch):
    """The adapter's sandbox auto-recovery goes through the same resolution."""
    from app.utils.working_directory import switch_conversation_cwd

    target = tmp_path / "recovered"
    target.mkdir()
    storage = _FakeStorage({SEAT_ROW: {
        "id": SEAT_ROW, "context_id": SEAT_CONTEXT, "profile": "member",
    }})

    try:
        asyncio.run(switch_conversation_cwd(
            SEAT_CONTEXT, str(target), storage, profile="member", publish=False,
        ))

        assert storage.updates == [(SEAT_ROW, {"working_directory": str(target)})]
        assert get_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY) == str(target)
    finally:
        clear_context(SEAT_CONTEXT, WORKING_DIR_OVERRIDE_KEY)
