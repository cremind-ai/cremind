"""The per-group live stream: sequence, replay window, subscribers.

Two differences from the conversation bus are the whole reason this class exists
separately, and both are pinned here: the replay ring is never cleared (a room
has no runs, only a continuous history, so a reconnecting client catches up from
it), and every subscriber to a group id gets every frame (no per-profile
routing — several profiles and the admin all watch the same room).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

import app.groups.bus as bus_module  # noqa: E402
from app.groups.bus import GroupStreamBus, get_group_stream_bus  # noqa: E402


def test_seq_counts_up_per_group_and_never_repeats() -> None:
    """The client uses ``seq`` to notice a gap; a shared counter would make one
    busy room look like a gap in every other."""
    bus = GroupStreamBus()

    async def run():
        await bus.publish("g1", "message", {"id": "m1"})
        await bus.publish("g2", "message", {"id": "m2"})
        await bus.publish("g1", "agent_status", {"profile": "dog", "state": "thinking"})

        assert [f["seq"] for f in bus.snapshot("g1")] == [1, 2]
        assert [f["type"] for f in bus.snapshot("g1")] == ["message", "agent_status"]
        assert [f["seq"] for f in bus.snapshot("g2")] == [1]

    asyncio.run(run())


def test_a_frame_always_has_the_three_keys_the_client_reads() -> None:
    bus = GroupStreamBus()

    async def run():
        await bus.publish("g1", "deleted")
        assert bus.snapshot("g1") == [{"seq": 1, "type": "deleted", "data": {}}]

    asyncio.run(run())


def test_publishing_to_no_group_is_a_no_op() -> None:
    """The fan-out publishes unconditionally; a missing id must not raise from
    the middle of a delivery."""
    bus = GroupStreamBus()

    async def run():
        await bus.publish("", "message", {"id": "m1"})
        assert bus.snapshot("") == []

    asyncio.run(run())


def test_the_replay_ring_is_capped_and_keeps_the_newest() -> None:
    """It is a rolling catch-up window, not a transcript — the timeline itself
    is in the database."""
    bus = GroupStreamBus()

    async def run():
        for index in range(bus_module._RING_CAP + 25):
            await bus.publish("g1", "message", {"id": index})

        ring = bus.snapshot("g1")
        assert len(ring) == bus_module._RING_CAP
        assert ring[0]["data"]["id"] == 25
        assert ring[-1]["data"]["id"] == bus_module._RING_CAP + 24
        # Sequence numbers keep climbing past the cap.
        assert ring[-1]["seq"] == bus_module._RING_CAP + 25

    asyncio.run(run())


def test_subscribing_replays_the_window_then_tails_live() -> None:
    """A client that reconnects mid-conversation gets what it missed and what
    happens next, in that order and with no duplicates."""
    bus = GroupStreamBus()

    async def run():
        await bus.publish("g1", "message", {"id": "before"})

        queue, replay = await bus.subscribe("g1")
        assert [f["data"]["id"] for f in replay] == ["before"]
        assert queue.empty()  # replay is handed over, not queued twice

        await bus.publish("g1", "message", {"id": "after"})
        assert (await queue.get())["data"]["id"] == "after"

    asyncio.run(run())


def test_every_subscriber_of_a_room_gets_every_frame() -> None:
    """A group belongs to several profiles plus the admin, so there is nothing
    to route — unlike a conversation, which belongs to one profile."""
    bus = GroupStreamBus()

    async def run():
        first, _ = await bus.subscribe("g1")
        second, _ = await bus.subscribe("g1")
        other_room, _ = await bus.subscribe("g2")

        await bus.publish("g1", "message", {"id": "m1"})

        assert (await first.get())["data"]["id"] == "m1"
        assert (await second.get())["data"]["id"] == "m1"
        assert other_room.empty()

    asyncio.run(run())


def test_unsubscribe_stops_delivery_and_tolerates_a_stranger() -> None:
    bus = GroupStreamBus()

    async def run():
        queue, _ = await bus.subscribe("g1")
        await bus.unsubscribe("g1", queue)

        await bus.publish("g1", "message", {"id": "m1"})
        assert queue.empty()
        # The frame is still in the ring for the next client to replay.
        assert len(bus.snapshot("g1")) == 1

        # Double unsubscribe, an unknown queue and an unknown group are all
        # ordinary teardown races, not errors.
        await bus.unsubscribe("g1", queue)
        await bus.unsubscribe("g1", asyncio.Queue())
        await bus.unsubscribe("nope", queue)

    asyncio.run(run())


def test_discard_forgets_the_room_entirely() -> None:
    """A deleted group must not leave its history sitting in memory, nor a
    sequence counter that would make a re-created id look mid-stream."""
    bus = GroupStreamBus()

    async def run():
        queue, _ = await bus.subscribe("g1")
        await bus.publish("g1", "message", {"id": "m1"})
        await queue.get()

        bus.discard("g1")

        assert bus.snapshot("g1") == []
        await bus.publish("g1", "message", {"id": "m2"})
        assert bus.snapshot("g1")[0]["seq"] == 1
        assert queue.empty()  # the old subscriber is detached
        bus.discard("nope")  # never an error

    asyncio.run(run())


def test_a_stalled_subscriber_does_not_break_the_publish() -> None:
    """Delivery to the room's members must not depend on one browser tab
    keeping up."""
    bus = GroupStreamBus()

    class _Stalled:
        def put_nowait(self, event):
            raise asyncio.QueueFull()

    async def run():
        healthy, _ = await bus.subscribe("g1")
        bus._subs["g1"].insert(0, _Stalled())

        await bus.publish("g1", "message", {"id": "m1"})

        assert (await healthy.get())["data"]["id"] == "m1"

    asyncio.run(run())


def test_an_ephemeral_frame_reaches_subscribers_but_not_the_ring() -> None:
    """A member's turn emits a frame per tool call. Live watchers want them; the
    catch-up window must not spend itself on them."""
    bus = GroupStreamBus()

    async def run():
        queue, _replay = await bus.subscribe("g1")

        await bus.publish("g1", "seat_event", {"profile": "dog"}, ephemeral=True)

        frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert frame["type"] == "seat_event"
        assert bus.snapshot("g1") == []

    asyncio.run(run())


def test_an_ephemeral_frame_cannot_evict_a_ringed_message() -> None:
    """The whole point: one busy turn would otherwise push every message the
    room actually holds out of a 200-entry window."""
    bus = GroupStreamBus()

    async def run():
        await bus.publish("g1", "message", {"id": "m1"})
        for _ in range(bus_module._RING_CAP * 2):
            await bus.publish("g1", "seat_event", {"profile": "dog"}, ephemeral=True)

        assert [f["data"]["id"] for f in bus.snapshot("g1")] == ["m1"]

    asyncio.run(run())


def test_an_ephemeral_frame_still_takes_a_seq_so_the_ring_has_gaps() -> None:
    """Skipping the counter would make two different frames share a seq, which a
    client dedupes as one. The client must therefore read a missing seq as a
    frame it was not meant to see, never as a dropped one."""
    bus = GroupStreamBus()

    async def run():
        await bus.publish("g1", "message", {"id": "m1"})
        await bus.publish("g1", "seat_event", {"profile": "dog"}, ephemeral=True)
        await bus.publish("g1", "message", {"id": "m2"})

        assert [f["seq"] for f in bus.snapshot("g1")] == [1, 3]

    asyncio.run(run())


def test_the_bus_is_a_process_wide_singleton() -> None:
    """The fan-out, the API's SSE endpoint and the turn-end status hook all
    reach for it independently."""
    assert get_group_stream_bus() is get_group_stream_bus()


def test_a_deleted_room_is_refused_for_good() -> None:
    """A room outlives its own deletion by one turn's worth of frames.

    A seat turn's closing status, the step frames mirrored off that turn and a
    fan-out that read the group a moment earlier all arrive after the row is
    gone. Each one used to re-create the sequence counter and the replay ring
    that nothing would pop again. The plain ``discard`` cannot enforce that —
    it is also how a caller throws away frames it does not want, and those
    rooms go on publishing.
    """
    bus = GroupStreamBus()

    async def run():
        queue, _ = await bus.subscribe("g1")
        await bus.publish("g1", "message", {"id": "m1"})
        await queue.get()

        bus.discard("g1", deleted=True)

        # The in-flight tail of the turn that was running when it was deleted.
        await bus.publish("g1", "agent_status", {"state": "idle"})
        await bus.publish("g1", "seat_event", {"profile": "dog"}, ephemeral=True)

        assert bus.snapshot("g1") == []
        assert bus._seq.get("g1") is None
        assert bus._ring.get("g1") is None

        # Clearing buffered state is NOT a deletion: that room still works.
        await bus.publish("g2", "message", {"id": "m2"})
        bus.discard("g2")
        await bus.publish("g2", "message", {"id": "m3"})
        assert [e["data"]["id"] for e in bus.snapshot("g2")] == ["m3"]

    asyncio.run(run())


def test_the_tombstone_list_cannot_grow_without_limit() -> None:
    """It sits on the path of every frame in every room, so it is bounded."""
    from app.groups.bus import _TOMBSTONE_CAP

    bus = GroupStreamBus()
    for n in range(_TOMBSTONE_CAP * 2):
        bus.discard(f"g{n}", deleted=True)

    assert len(bus._deleted) == _TOMBSTONE_CAP
