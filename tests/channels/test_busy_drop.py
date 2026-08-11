"""Mid-turn message dropping — the channel analog of the web UI's disabled
send button.

While a turn is in flight for a sender, a new message is acked once with
"I'm thinking…" and **dropped**: it is never dispatched, so it is never
persisted, never shown in the web UI, and never enters the model's history.
Only the user's own message and the ack remain visible on the channel. When the
turn finishes the sender can chat again and the ack re-arms.

Drives the real ``BaseChannelAdapter._handle_inbound`` and
``_dispatch_to_agent`` (so the real ``_inflight`` registration and
``_clear_inflight`` reset run) with a fake storage, a controllable
``_forward_reply`` that holds the turn "in flight" until released, and a stubbed
event queue. Everything runs in a single event loop per test so background
forwarder tasks survive across messages.
"""

from __future__ import annotations

import asyncio

import pytest

import app.channels.base as base_mod
from app.channels.base import BaseChannelAdapter


class _Storage:
    """Minimal fake: open auth (no config), one stable conversation per sender."""

    _n = 0

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None):
        _Storage._n += 1
        return {
            "id": f"s{_Storage._n}", "channel_id": channel_id, "sender_id": sender_id,
            "display_name": display_name, "authenticated": True,
            "conversation_id": f"conv-{sender_id}",
        }

    async def get_conversation(self, conv_id):
        return {"id": conv_id}

    async def ensure_sender_conversation(self, sender, profile, channel_id,
                                         display_name=None):
        return sender["conversation_id"]

    async def get_messages(self, conversation_id):
        # Empty history → _dispatch_to_agent skips the history-building block
        # (and thus replay_reasoning_enabled / user config) entirely.
        return []


class _ConvAdapter(BaseChannelAdapter):
    def __init__(self, channel, storage):
        super().__init__(channel, storage)
        self.sent: list[tuple[str, str]] = []
        self.forwards = 0
        self.release = asyncio.Event()

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append((sender_id, text))

    async def _forward_reply(self, conversation_id, sender_id):
        # Stand in for a real run: stay "in flight" until the test releases us.
        self.forwards += 1
        await self.release.wait()


@pytest.fixture(autouse=True)
def _stub_queue(monkeypatch):
    """Neutralize the agent enqueue so dispatch only creates the forwarder."""

    async def _noop(**kwargs):
        return None

    monkeypatch.setattr(base_mod.event_queue, "enqueue_user_message", _noop)


def _adapter() -> _ConvAdapter:
    channel = {
        "id": "c1", "profile": "admin", "channel_type": "telegram",
        "mode": "bot", "config": {},
    }
    return _ConvAdapter(channel, _Storage())


async def _settle():
    # Let freshly-created tasks start and done-callbacks (call_soon) flush.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _finish_run(a: _ConvAdapter, sender_id: str):
    """Release the in-flight forwarder and wait for cleanup to run."""
    task = a._inflight.get(sender_id)
    a.release.set()
    if task is not None:
        await task
    await _settle()
    a.release.clear()


def test_free_sender_dispatches_without_ack():
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")
        await _settle()
        assert a.forwards == 1                       # dispatched: forwarder started
        assert not a._inflight["u1"].done()          # and still in flight
        assert a.sent == []                          # no "I'm thinking…" ack
        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_mid_turn_messages_dropped_with_single_ack():
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")   # in flight
        await _settle()
        assert a.forwards == 1

        # Two more while busy: both dropped, exactly one ack.
        await a._handle_inbound("u1", "Tester", "second")
        await a._handle_inbound("u1", "Tester", "third")
        await _settle()
        assert a.forwards == 1                        # neither was dispatched
        assert a.sent == [("u1", "I'm thinking…")]    # single ack for the burst

        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_ack_rearms_after_turn_completes():
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")   # run 1 in flight
        await _settle()
        await a._handle_inbound("u1", "Tester", "second")  # dropped + ack #1
        await _settle()
        assert a.forwards == 1 and len(a.sent) == 1

        await _finish_run(a, "u1")                    # run 1 done → throttle reset
        assert "u1" not in a._inflight

        await a._handle_inbound("u1", "Tester", "third")   # free → run 2 dispatched
        await _settle()
        assert a.forwards == 2
        await a._handle_inbound("u1", "Tester", "fourth")  # busy again → ack #2
        await _settle()
        assert a.forwards == 2                        # still not dispatched
        assert a.sent == [("u1", "I'm thinking…"), ("u1", "I'm thinking…")]

        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_distinct_senders_are_independent():
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "One", "hi")    # u1 in flight
        await a._handle_inbound("u2", "Two", "hi")    # different sender: dispatches
        await _settle()
        assert a.forwards == 2                         # both dispatched
        assert a.sent == []                            # neither got an ack
        await _finish_run(a, "u1")
        await _finish_run(a, "u2")

    asyncio.run(scenario())
