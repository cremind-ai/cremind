"""A message the agent chose not to answer, and what the UI is told about it.

Storing it is the easy half. The hard half is telling an open web view about it
without telling the view that a run has started — because a ``user_message``
frame IS "a run has started" to every client there is, and no terminal frame is
ever coming for a message nobody answered. The symptom was a permanent "Agent is
thinking…", re-armed after every reply the agent DID make (in a two-agent group
each agent's answer is the other's unanswered message), and surviving a close
and reopen because the frame had been kept for replay.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels.groups.dispatch import _quiet_write
from app.events.stream_bus import ConversationStreamBus
from tests.channels.groups.conftest import make_adapter


def _metadata(decision: str = "judge:irrelevant") -> dict:
    return {
        "source": "channel_group",
        "channel_group": {"group_id": "g-1", "decision": decision, "quiet": True},
    }


def _bus_and_adapter(monkeypatch):
    bus = ConversationStreamBus()
    import app.events as events_mod

    monkeypatch.setattr(events_mod, "get_event_stream_bus", lambda: bus)
    return bus, make_adapter()


# ── the frame ─────────────────────────────────────────────────────────────


def test_the_message_is_stored(monkeypatch):
    bus, adapter = _bus_and_adapter(monkeypatch)
    asyncio.run(_quiet_write(adapter, "conv-1", "Alexa: morning", _metadata()))

    (row,) = adapter.storage.messages
    assert row["role"] == "user"
    assert row["content"] == "Alexa: morning"


def test_a_watching_client_is_told(monkeypatch):
    """The point of publishing at all: a group's conversation that went quiet
    for ten minutes should show the ten minutes of traffic, not look asleep."""
    bus, adapter = _bus_and_adapter(monkeypatch)

    async def _run():
        queue, _replay, _active = await bus.subscribe("conv-1")
        await _quiet_write(adapter, "conv-1", "Alexa: morning", _metadata())
        return queue.get_nowait()

    frame = asyncio.run(_run())
    assert frame["type"] == "quiet_user_message"
    assert frame["data"]["content"] == "Alexa: morning"


def test_it_is_not_a_user_message_frame(monkeypatch):
    """The whole bug. ``user_message`` means "a run is starting" — the web store
    sets ``isStreaming`` on one, and only ``complete`` or ``error`` clears it.
    Neither is coming."""
    bus, adapter = _bus_and_adapter(monkeypatch)

    async def _run():
        queue, _replay, _active = await bus.subscribe("conv-1")
        await _quiet_write(adapter, "conv-1", "Alexa: morning", _metadata())
        return queue.get_nowait()

    assert asyncio.run(_run())["type"] != "user_message"


def test_it_does_not_make_the_conversation_look_active(monkeypatch):
    bus, adapter = _bus_and_adapter(monkeypatch)
    asyncio.run(_quiet_write(adapter, "conv-1", "Alexa: morning", _metadata()))
    assert bus.is_active("conv-1") is False


def test_it_is_not_kept_for_replay(monkeypatch):
    """The ring belongs to the current run: ``start_run`` clears it and
    ``end_run`` empties it. A frame appended outside a run is cleared by
    nothing, so every later subscriber replays it — which is why the stuck
    indicator came back after closing and reopening the conversation."""
    bus, adapter = _bus_and_adapter(monkeypatch)

    async def _run():
        await _quiet_write(adapter, "conv-1", "Alexa: morning", _metadata())
        _queue, replay, _active = await bus.subscribe("conv-1")
        return replay

    assert asyncio.run(_run()) == []


def test_a_real_run_still_replays(monkeypatch):
    """The transient path must not have broken the ordinary one."""
    bus, _adapter = _bus_and_adapter(monkeypatch)

    async def _run():
        await bus.start_run("conv-1", "admin")
        await bus.publish("conv-1", "user_message", {"content": "hi"})
        _queue, replay, active = await bus.subscribe("conv-1")
        return replay, active

    replay, active = asyncio.run(_run())
    assert [f["type"] for f in replay] == ["user_message"]
    assert active is True


def test_the_frame_carries_why_it_went_unanswered(monkeypatch):
    """So the transcript can say "no reply — not addressed to this agent"
    rather than leaving a question hanging with no explanation."""
    bus, adapter = _bus_and_adapter(monkeypatch)

    async def _run():
        queue, _replay, _active = await bus.subscribe("conv-1")
        await _quiet_write(
            adapter, "conv-1", "Alexa: morning", _metadata("judge:irrelevant"),
        )
        return queue.get_nowait()

    stamp = asyncio.run(_run())["data"]["metadata"]["channel_group"]
    assert stamp["quiet"] is True
    assert stamp["decision"] == "judge:irrelevant"


# ── reaching a client that is not subscribed to this conversation ─────────


def test_it_reaches_the_profile_stream_before_any_run_has_happened(monkeypatch):
    """The bus learns a conversation's profile at ``start_run``. A group whose
    first message is one nobody answers has never had a run, so without an
    explicit profile the frame would reach nobody — which is every group's
    first quiet message."""
    bus, adapter = _bus_and_adapter(monkeypatch)
    sent: list = []

    class _Fanout:
        async def publish(self, profile, conversation_id, event):
            sent.append((profile, conversation_id, event["type"]))

    import app.events.stream_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_profile_stream_fanout", lambda: _Fanout())
    asyncio.run(_quiet_write(adapter, "conv-1", "Alexa: morning", _metadata()))

    assert sent == [("admin", "conv-1", "quiet_user_message")]


def test_a_storage_failure_does_not_reach_the_caller(monkeypatch):
    """This runs under the group's inbound lock; raising would drop the next
    message in that group too."""
    bus, adapter = _bus_and_adapter(monkeypatch)

    async def _boom(**_kw):
        raise RuntimeError("disk is on fire")

    adapter.storage.add_message = _boom
    asyncio.run(_quiet_write(adapter, "conv-1", "Alexa: morning", _metadata()))
