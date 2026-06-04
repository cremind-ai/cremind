"""Tests for ProfileStreamFanoutBus and its integration with ConversationStreamBus.

The fanout bus is what lets the multiplexed ``/api/profile-events/stream``
endpoint carry per-conversation events without each browser tab needing
its own HTTP/1.1 connection per conversation.
"""

from __future__ import annotations

import asyncio

import pytest

from app.events.profile_stream_fanout import ProfileStreamFanoutBus
from app.events.stream_bus import ConversationStreamBus


def test_publish_fans_to_subscribed_profile():
    async def run():
        bus = ProfileStreamFanoutBus()
        queue = await bus.subscribe("alice")
        await bus.publish("alice", "conv-1", {"seq": 1, "type": "text", "data": {"token": "hi"}})
        envelope = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert envelope == {
            "conversation_id": "conv-1",
            "seq": 1,
            "type": "text",
            "data": {"token": "hi"},
        }

    asyncio.run(run())


def test_two_profiles_do_not_cross_pollinate():
    async def run():
        bus = ProfileStreamFanoutBus()
        a_queue = await bus.subscribe("alice")
        b_queue = await bus.subscribe("bob")

        await bus.publish("alice", "conv-1", {"seq": 1, "type": "text", "data": {}})
        await bus.publish("bob", "conv-9", {"seq": 1, "type": "text", "data": {}})

        a_env = await asyncio.wait_for(a_queue.get(), timeout=1.0)
        b_env = await asyncio.wait_for(b_queue.get(), timeout=1.0)
        assert a_env["conversation_id"] == "conv-1"
        assert b_env["conversation_id"] == "conv-9"

        # Each profile's queue should now be empty — no cross-talk.
        assert a_queue.empty()
        assert b_queue.empty()

    asyncio.run(run())


def test_late_subscriber_only_sees_future_events():
    async def run():
        bus = ProfileStreamFanoutBus()
        await bus.publish("alice", "conv-1", {"seq": 1, "type": "text", "data": {}})
        # Subscribe AFTER the publish — the late subscriber should not see it.
        queue = await bus.subscribe("alice")
        assert queue.empty()

        await bus.publish("alice", "conv-1", {"seq": 2, "type": "text", "data": {}})
        envelope = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert envelope["seq"] == 2

    asyncio.run(run())


def test_publish_with_no_subscribers_is_a_noop():
    async def run():
        bus = ProfileStreamFanoutBus()
        # Should not raise / hang.
        await bus.publish("alice", "conv-1", {"seq": 1, "type": "text", "data": {}})

    asyncio.run(run())


def test_unsubscribe_stops_delivery():
    async def run():
        bus = ProfileStreamFanoutBus()
        queue = await bus.subscribe("alice")
        await bus.unsubscribe("alice", queue)
        await bus.publish("alice", "conv-1", {"seq": 1, "type": "text", "data": {}})
        assert queue.empty()

    asyncio.run(run())


def test_unsubscribe_unknown_queue_is_safe():
    async def run():
        bus = ProfileStreamFanoutBus()
        queue = await bus.subscribe("alice")
        await bus.unsubscribe("alice", queue)
        # Calling again should be idempotent.
        await bus.unsubscribe("alice", queue)
        # Unknown profile entirely.
        await bus.unsubscribe("nobody", queue)

    asyncio.run(run())


def test_stream_bus_publish_fans_to_profile_bus(monkeypatch):
    """ConversationStreamBus.publish() should also fan to the profile bus
    when start_run was called with a profile."""
    fanout = ProfileStreamFanoutBus()
    monkeypatch.setattr(
        "app.events.stream_bus.get_profile_stream_fanout", lambda: fanout,
    )

    async def run():
        conv_bus = ConversationStreamBus()
        profile_queue = await fanout.subscribe("alice")

        await conv_bus.start_run("conv-1", profile="alice")
        await conv_bus.publish("conv-1", "text", {"token": "hello"})

        envelope = await asyncio.wait_for(profile_queue.get(), timeout=1.0)
        assert envelope["conversation_id"] == "conv-1"
        assert envelope["type"] == "text"
        assert envelope["data"] == {"token": "hello"}
        assert envelope["seq"] == 1

    asyncio.run(run())


def test_stream_bus_publish_without_start_run_skips_fanout(monkeypatch):
    """If no start_run was called for a conversation, publish should NOT
    fan to the profile bus (no profile is known)."""
    fanout = ProfileStreamFanoutBus()
    monkeypatch.setattr(
        "app.events.stream_bus.get_profile_stream_fanout", lambda: fanout,
    )

    async def run():
        conv_bus = ConversationStreamBus()
        profile_queue = await fanout.subscribe("alice")

        # Publish without start_run — per-conversation subscribers (none here)
        # would still receive, but the profile fanout should be skipped.
        await conv_bus.publish("conv-orphan", "text", {"token": "x"})

        # Confirm nothing arrives within a short window.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(profile_queue.get(), timeout=0.1)

    asyncio.run(run())


def test_stream_bus_snapshot_for_profile():
    async def run():
        conv_bus = ConversationStreamBus()
        await conv_bus.start_run("conv-1", profile="alice")
        await conv_bus.publish("conv-1", "text", {"token": "a"})
        await conv_bus.start_run("conv-2", profile="alice")
        await conv_bus.publish("conv-2", "text", {"token": "b"})
        await conv_bus.start_run("conv-9", profile="bob")
        await conv_bus.publish("conv-9", "text", {"token": "c"})

        snap = await conv_bus.snapshot_for_profile("alice")
        snap_dict = {cid: ring for cid, ring in snap}
        assert "conv-1" in snap_dict
        assert "conv-2" in snap_dict
        assert "conv-9" not in snap_dict
        assert snap_dict["conv-1"][0]["data"] == {"token": "a"}

    asyncio.run(run())


def test_stream_bus_snapshot_excludes_ended_runs():
    async def run():
        conv_bus = ConversationStreamBus()
        await conv_bus.start_run("conv-1", profile="alice")
        await conv_bus.publish("conv-1", "text", {"token": "a"})
        await conv_bus.end_run("conv-1")

        snap = await conv_bus.snapshot_for_profile("alice")
        assert snap == []

    asyncio.run(run())


def test_discard_invalidates_profile_cache(monkeypatch):
    fanout = ProfileStreamFanoutBus()
    monkeypatch.setattr(
        "app.events.stream_bus.get_profile_stream_fanout", lambda: fanout,
    )

    async def run():
        conv_bus = ConversationStreamBus()
        profile_queue = await fanout.subscribe("alice")

        await conv_bus.start_run("conv-1", profile="alice")
        await conv_bus.publish("conv-1", "text", {"token": "hi"})
        # Drain the expected fanout event.
        await asyncio.wait_for(profile_queue.get(), timeout=1.0)

        await conv_bus.discard("conv-1")

        # After discard the profile cache is gone — a subsequent publish
        # should NOT fan to the profile bus (matches the "no start_run"
        # contract).
        await conv_bus.publish("conv-1", "text", {"token": "ghost"})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(profile_queue.get(), timeout=0.1)

    asyncio.run(run())
