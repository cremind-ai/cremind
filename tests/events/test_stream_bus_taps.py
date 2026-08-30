"""The conversation bus's tap registry: a third audience for a turn's frames.

A conversation's frames go to its subscribers and to the owning profile's
multiplexed stream. A group-chat seat needs them somewhere neither of those
reaches — the room's own stream — and the tap is how, attached for the length of
one turn.

Two properties are the reason it lives at the bus rather than at the publishing
call sites: it sees frames published from anywhere (``cwd`` alone comes from four
modules), and a tap that misbehaves is the tap's problem, never the turn's.
"""

from __future__ import annotations

import asyncio

from app.events.stream_bus import ConversationStreamBus


def test_a_tap_sees_every_frame_of_the_conversation_it_is_attached_to():
    async def run():
        bus = ConversationStreamBus()
        seen: list = []

        async def tap(event):
            seen.append(event)

        await bus.add_tap("conv-1", tap)
        await bus.publish("conv-1", "thinking", {"Step": 1})
        await bus.publish("conv-1", "complete", {"errored": False})
        # A different conversation is a different registry entry.
        await bus.publish("conv-2", "thinking", {"Step": 1})

        assert [e["type"] for e in seen] == ["thinking", "complete"]
        # The whole frame, seq included — the same object subscribers get.
        assert seen[0] == {"seq": 1, "type": "thinking", "data": {"Step": 1}}

    asyncio.run(run())


def test_a_synchronous_tap_works_too():
    """The registry is generic; only the group mirror happens to be async."""

    async def run():
        bus = ConversationStreamBus()
        seen: list = []
        await bus.add_tap("conv-1", seen.append)
        await bus.publish("conv-1", "cwd", {"working_directory": "/tmp"})
        assert seen[0]["data"] == {"working_directory": "/tmp"}

    asyncio.run(run())


def test_removing_a_tap_stops_delivery_and_tolerates_a_stranger():
    async def run():
        bus = ConversationStreamBus()
        seen: list = []

        async def tap(event):
            seen.append(event)

        await bus.add_tap("conv-1", tap)
        await bus.publish("conv-1", "thinking", {"Step": 1})
        await bus.remove_tap("conv-1", tap)
        await bus.publish("conv-1", "thinking", {"Step": 2})

        assert [e["data"]["Step"] for e in seen] == [1]
        # Double removal, an unknown tap and an unknown conversation are all
        # ordinary teardown races, not errors.
        await bus.remove_tap("conv-1", tap)
        await bus.remove_tap("conv-1", lambda _e: None)
        await bus.remove_tap("nope", tap)

    asyncio.run(run())


def test_a_raising_tap_never_breaks_the_publish():
    """The turn's own subscribers must not depend on a bolt-on audience."""

    async def run():
        bus = ConversationStreamBus()
        seen: list = []

        async def exploding(_event):
            raise RuntimeError("boom")

        def exploding_sync(_event):
            raise RuntimeError("boom, synchronously")

        async def healthy(event):
            seen.append(event)

        queue, _replay, _active = await bus.subscribe("conv-1")
        await bus.add_tap("conv-1", exploding)
        await bus.add_tap("conv-1", exploding_sync)
        await bus.add_tap("conv-1", healthy)

        event = await bus.publish("conv-1", "result", {"step": 1})

        assert event["type"] == "result"
        assert (await queue.get())["type"] == "result"
        # A failure in one tap does not skip the ones behind it.
        assert [e["type"] for e in seen] == ["result"]

    asyncio.run(run())


def test_taps_run_after_the_subscribers_have_been_served():
    """A tap that blocks (a slow group bus) must not delay the conversation's
    own UI, so it is handed the frame last."""

    async def run():
        bus = ConversationStreamBus()
        queued_when_tapped: list = []

        async def tap(_event):
            queued_when_tapped.append(queue.qsize())

        queue, _replay, _active = await bus.subscribe("conv-1")
        await bus.add_tap("conv-1", tap)
        await bus.publish("conv-1", "text", {"token": "hi"})

        assert queued_when_tapped == [1]

    asyncio.run(run())


def test_snapshot_returns_the_ring_and_whether_a_run_is_live():
    """The read half of ``subscribe``, for a client watching a different stream
    (the room, catching up on a member's in-flight turn)."""

    async def run():
        bus = ConversationStreamBus()
        assert await bus.snapshot("conv-1") == ([], False)

        await bus.start_run("conv-1", "dog")
        await bus.publish("conv-1", "thinking", {"Step": 1})
        ring, active = await bus.snapshot("conv-1")
        assert active is True
        assert [e["data"]["Step"] for e in ring] == [1]

        # Snapshotting does not subscribe: the next frame is not held for us.
        await bus.publish("conv-1", "thinking", {"Step": 2})
        assert len((await bus.snapshot("conv-1"))[0]) == 2

        await bus.end_run("conv-1")
        assert await bus.snapshot("conv-1") == ([], False)

    asyncio.run(run())


def test_discard_drops_the_taps_with_everything_else():
    """A deleted group's seat is discarded from under its own turn; a tap left
    behind would keep mirroring frames into a room that no longer exists."""

    async def run():
        bus = ConversationStreamBus()
        seen: list = []

        async def tap(event):
            seen.append(event)

        await bus.add_tap("conv-1", tap)
        await bus.discard("conv-1")
        await bus.publish("conv-1", "thinking", {"Step": 1})

        assert seen == []

    asyncio.run(run())


def test_a_tap_needs_both_an_id_and_a_callable():
    async def run():
        bus = ConversationStreamBus()
        await bus.add_tap("", lambda _e: None)
        await bus.add_tap("conv-1", None)
        assert bus._taps == {}

    asyncio.run(run())
