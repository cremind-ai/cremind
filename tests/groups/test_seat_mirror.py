"""Mirroring a seat's live frames onto the room's stream.

A member's turn already publishes everything the room wants to show — the tool
it called, what came back, where it is working — but only to the profile that
owns the seat, and the room watches ONE group stream rather than a conversation
stream per member. So the frames are tapped at the conversation bus and
re-published on the group bus, tagged with the member they came from.

What is pinned here is the shape of that translation: which frame types cross
(the allowlist is a privacy and a noise decision at once), that a non-seat
conversation binds nothing at all, that one conversation frame produces exactly
one room frame, and that unbinding really stops it — a tap left attached would
narrate the next turn under a run the room already watched finish.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

import app.groups.bus as bus_module  # noqa: E402
from app.events.stream_bus import ConversationStreamBus  # noqa: E402
from app.groups.bus import GroupStreamBus  # noqa: E402
from app.groups.hooks import (  # noqa: E402
    SEAT_EVENT_TYPES,
    bind_seat_mirror,
    seat_event_payload,
    unbind_seat_mirror,
)

_SEAT = {"id": "conv-dog", "context_id": "group:g1:dog", "kind": "group_chat"}


def _buses(monkeypatch):
    """Fresh conversation + group buses, wired in as the process singletons."""
    conv_bus = ConversationStreamBus()
    group_bus = GroupStreamBus()
    monkeypatch.setattr("app.events.stream_bus._instance", conv_bus)
    monkeypatch.setattr(bus_module, "_instance", group_bus)
    return conv_bus, group_bus


# ── the payload ────────────────────────────────────────────────────────────


def test_the_payload_names_the_member_the_frame_came_from():
    """The room shows several agents at once, and the SSE endpoint gates on this
    profile — a frame that did not say whose it was could not be filtered."""
    payload = seat_event_payload("dog", "conv-dog", {
        "seq": 7, "type": "thinking", "data": {"Step": 2, "Tool": "exec_shell"},
    })
    assert payload == {
        "profile": "dog",
        "conversation_id": "conv-dog",
        "type": "thinking",
        "seat_seq": 7,
        "data": {"Step": 2, "Tool": "exec_shell"},
    }


def test_the_payload_carries_the_seats_own_seq():
    """A client joining a busy room is caught up from the seat's ring and then
    hears the live tail, so a frame published between the two arrives twice.
    The seat's sequence number is what makes recognising it exact rather than a
    guess from the step's contents — two identical parallel tool calls are
    indistinguishable any other way."""
    first = seat_event_payload("dog", "conv-dog", {
        "seq": 4, "type": "thinking", "data": {},
    })
    second = seat_event_payload("dog", "conv-dog", {
        "seq": 5, "type": "thinking", "data": {},
    })
    assert (first["seat_seq"], second["seat_seq"]) == (4, 5)
    # A frame from a publisher that stamps no seq still crosses; the client
    # falls back to identifying the step by its contents.
    assert seat_event_payload(
        "dog", "conv-dog", {"type": "thinking", "data": {}},
    )["seat_seq"] is None


def test_every_allowlisted_type_crosses():
    for event_type in SEAT_EVENT_TYPES:
        payload = seat_event_payload(
            "dog", "conv-dog", {"seq": 1, "type": event_type, "data": {}},
        )
        assert payload is not None and payload["type"] == event_type


def test_text_tokens_never_cross():
    """The room renders whole messages, posted at turn end. Streaming the tokens
    too would race that post and show every answer twice."""
    assert "text" not in SEAT_EVENT_TYPES
    assert seat_event_payload(
        "dog", "conv-dog", {"seq": 1, "type": "text", "data": {"token": "hi"}},
    ) is None


def test_the_frames_that_address_one_client_never_cross():
    """A mid-turn injection, a flow break or a plan-mode question is a
    conversation between one agent and one person; a spectator can neither act
    on it nor make sense of it."""
    for event_type in ("user_message", "flow_break", "todos", "plan_decision",
                       "event_trigger_message", "file", "compaction_suggested"):
        assert seat_event_payload(
            "dog", "conv-dog", {"seq": 1, "type": event_type, "data": {}},
        ) is None


def test_a_malformed_frame_is_dropped_rather_than_mirrored():
    assert seat_event_payload("dog", "conv-dog", None) is None
    assert seat_event_payload("dog", "conv-dog", {"seq": 1}) is None
    assert seat_event_payload("", "conv-dog", {"type": "thinking"}) is None
    assert seat_event_payload("dog", "", {"type": "thinking"}) is None
    # A frame with no data still crosses — "complete" carries nothing useful and
    # is exactly the one the room needs to stop spinning.
    assert seat_event_payload(
        "dog", "conv-dog", {"seq": 1, "type": "complete"},
    )["data"] == {}


# ── binding ────────────────────────────────────────────────────────────────


def test_a_seat_frame_becomes_exactly_one_room_frame(monkeypatch):
    conv_bus, group_bus = _buses(monkeypatch)

    async def run():
        queue, _replay = await group_bus.subscribe("g1")
        tap = await bind_seat_mirror(_SEAT, "dog")
        assert tap is not None

        await conv_bus.publish("conv-dog", "thinking", {"Step": 1, "Tool": "shell"})

        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert frame["type"] == "seat_event"
        assert frame["data"] == {
            "profile": "dog",
            "conversation_id": "conv-dog",
            "type": "thinking",
            "seat_seq": 1,
            "data": {"Step": 1, "Tool": "shell"},
        }
        assert queue.empty()

    asyncio.run(run())


def test_the_mirrored_frames_are_ephemeral(monkeypatch):
    """One busy turn emits a frame per tool call; ringing them would evict the
    room's actual messages out of a window nobody could then catch up from."""
    conv_bus, group_bus = _buses(monkeypatch)

    async def run():
        await group_bus.publish("g1", "message", {"id": "m1"})
        await bind_seat_mirror(_SEAT, "dog")
        for step in range(5):
            await conv_bus.publish("conv-dog", "thinking", {"Step": step})

        assert [f["type"] for f in group_bus.snapshot("g1")] == ["message"]

    asyncio.run(run())


def test_a_non_seat_conversation_binds_nothing(monkeypatch):
    """Every ordinary turn calls this; it must cost nothing and leave no tap."""
    conv_bus, group_bus = _buses(monkeypatch)

    async def run():
        assert await bind_seat_mirror(
            {"id": "conv-1", "context_id": "conv-1", "kind": "chat"}, "dog",
        ) is None
        assert await bind_seat_mirror(None, "dog") is None
        # A seat row with no id yet is not bindable either — the tap is keyed
        # by the conversation whose frames it wants.
        assert await bind_seat_mirror({"context_id": "group:g1:dog"}, "dog") is None
        assert conv_bus._taps == {}

    asyncio.run(run())


def test_unbinding_stops_the_mirror(monkeypatch):
    conv_bus, group_bus = _buses(monkeypatch)

    async def run():
        queue, _replay = await group_bus.subscribe("g1")
        tap = await bind_seat_mirror(_SEAT, "dog")

        await conv_bus.publish("conv-dog", "complete", {"errored": False})
        await asyncio.wait_for(queue.get(), timeout=1.0)

        await unbind_seat_mirror("conv-dog", tap)
        await conv_bus.publish("conv-dog", "thinking", {"Step": 99})

        assert queue.empty()
        assert conv_bus._taps == {}
        # Unbinding twice, or a turn that never bound, is ordinary teardown.
        await unbind_seat_mirror("conv-dog", tap)
        await unbind_seat_mirror("conv-dog", None)

    asyncio.run(run())


def test_two_members_mirror_into_the_same_room_under_their_own_names(monkeypatch):
    conv_bus, group_bus = _buses(monkeypatch)

    async def run():
        queue, _replay = await group_bus.subscribe("g1")
        await bind_seat_mirror(_SEAT, "dog")
        await bind_seat_mirror(
            {"id": "conv-cat", "context_id": "group:g1:cat"}, "cat",
        )

        await conv_bus.publish("conv-dog", "result", {"step": 1})
        await conv_bus.publish("conv-cat", "result", {"step": 1})

        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        second = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert [f["data"]["profile"] for f in (first, second)] == ["dog", "cat"]

    asyncio.run(run())


def test_a_broken_group_bus_never_fails_the_turn(monkeypatch):
    """The mirror is a spectator feature; the member's own run outranks it."""
    conv_bus, _group_bus = _buses(monkeypatch)

    class _Broken:
        async def publish(self, *args, **kwargs):
            raise RuntimeError("no room")

    async def run():
        await bind_seat_mirror(_SEAT, "dog")
        monkeypatch.setattr(bus_module, "_instance", _Broken())
        # The publish still returns its frame to the caller.
        event = await conv_bus.publish("conv-dog", "thinking", {"Step": 1})
        assert event["type"] == "thinking"

    asyncio.run(run())
