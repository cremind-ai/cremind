"""Mid-turn inbound messages — folded into the running turn, never dropped.

Replaces the old busy-drop behaviour, where a message arriving while a turn was
in flight got a cosmetic "I'm thinking…" and was thrown away (never persisted,
never in the model's history). Now every message is dispatched: one arriving
mid-turn is parked and injected into the running agent, so the reply being
written accounts for it.

The forwarder invariant that dropping used to buy for free is now explicit: at
most ONE forwarder is live per sender, and because a forwarder ends at the first
terminal event it absorbs, a run that starts while one is live gets a CHAINED
forwarder when that one finishes — never a second concurrent subscriber that
could absorb the wrong run's tail.

Drives the real ``_handle_inbound`` / ``_dispatch_to_agent`` / ``_expect_run``
with a fake storage, a controllable ``_forward_reply`` that holds a turn "in
flight" until released, and a stubbed event queue. One event loop per test so
background forwarder tasks survive across messages.
"""

from __future__ import annotations

import asyncio

import pytest

import app.channels.base as base_mod
from app.channels.base import BaseChannelAdapter
from app.events.user_message_delivery import ParkOutcome


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

    async def list_senders(self, channel_id):
        return [{"sender_id": "u1", "conversation_id": "conv-u1"}]


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
    calls: list[dict] = []

    async def _noop(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(base_mod.event_queue, "enqueue_user_message", _noop)
    return calls


@pytest.fixture
def park(monkeypatch):
    """Control what the mid-turn park decides, per message."""
    state = {"outcomes": [], "calls": []}

    async def _fake_park(**kwargs):
        state["calls"].append(kwargs)
        if state["outcomes"]:
            return state["outcomes"].pop(0)
        return None  # idle: caller runs it as its own turn

    import app.events.user_message_delivery as umd
    monkeypatch.setattr(umd, "try_park_user_message", _fake_park)
    return state


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


def test_idle_sender_dispatches_a_run(park):
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")
        await _settle()
        assert a.forwards == 1                       # dispatched: forwarder started
        assert not a._inflight["u1"].done()          # and still in flight
        assert a.sent == []                          # nothing cosmetic sent
        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_mid_turn_message_is_injected_not_acked(park, _stub_queue):
    """The whole point: no ack, no drop, no second run — it joins the live turn."""
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")   # idle → run 1
        await _settle()
        assert a.forwards == 1

        park["outcomes"] = [
            ParkOutcome(injected=True, message_id="m2", run_id="r1"),
            ParkOutcome(injected=True, message_id="m3", run_id="r1"),
        ]
        await a._handle_inbound("u1", "Tester", "second")
        await a._handle_inbound("u1", "Tester", "third")
        await _settle()

        assert a.forwards == 1              # no extra forwarder for injected msgs
        assert a.sent == []                 # and no "I'm thinking…"
        assert len(_stub_queue) == 1        # only the first message started a run
        # Both were offered to the running turn, with the channel metadata.
        assert [c["query"] for c in park["calls"]] == ["first", "second", "third"]
        assert park["calls"][1]["user_message_metadata"]["source"] == "channel"

        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_injected_message_arms_a_forwarder_when_none_is_live(park):
    """A run started elsewhere (skill event, task flush) still needs delivery."""
    async def scenario():
        a = _adapter()
        park["outcomes"] = [ParkOutcome(injected=True, message_id="m1", run_id="r9")]
        await a._handle_inbound("u1", "Tester", "hello")
        await _settle()

        assert a.forwards == 1                       # exactly one, armed for the run
        assert a._pending_runs.get("u1") == 1
        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_a_second_run_gets_a_chained_forwarder(park, _stub_queue):
    """Two runs, never two concurrent forwarders — the second is chained."""
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")    # run 1
        await _settle()
        assert a.forwards == 1

        # Park refuses (turn ending): this becomes a real second run while
        # forwarder 1 is still live.
        await a._handle_inbound("u1", "Tester", "second")
        await _settle()
        assert a.forwards == 1                  # still only one live forwarder
        assert a._pending_runs["u1"] == 2       # but two runs are owed one
        assert len(_stub_queue) == 2

        await _finish_run(a, "u1")              # forwarder 1 ends → chain forwarder 2
        assert a.forwards == 2
        assert a._pending_runs.get("u1") == 1

        await _finish_run(a, "u1")
        assert "u1" not in a._pending_runs      # drained
        assert "u1" not in a._inflight

    asyncio.run(scenario())


def test_race_lost_park_does_not_persist_twice(park, _stub_queue):
    """A parked-then-released message runs as its own turn without a second row."""
    async def scenario():
        a = _adapter()
        park["outcomes"] = [ParkOutcome(injected=False, message_id="m7")]
        await a._handle_inbound("u1", "Tester", "hello")
        await _settle()

        assert len(_stub_queue) == 1
        assert _stub_queue[0]["push_user_message"] is False
        assert _stub_queue[0]["existing_user_message_id"] == "m7"
        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_forward_external_run_chains_instead_of_skipping(park):
    """A live forwarder ends at ITS run's terminal event, so it cannot cover another."""
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "Tester", "first")
        await _settle()
        assert a.forwards == 1

        await a.forward_external_run("conv-u1")
        await _settle()
        assert a.forwards == 1                  # not spawned concurrently
        assert a._pending_runs["u1"] == 2       # queued behind the live one

        await _finish_run(a, "u1")
        assert a.forwards == 2                  # chained once the first finished
        await _finish_run(a, "u1")

    asyncio.run(scenario())


def test_enqueue_failure_releases_the_expected_run(park, monkeypatch):
    async def scenario():
        a = _adapter()

        async def _boom(**kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(base_mod.event_queue, "enqueue_user_message", _boom)
        await a._handle_inbound("u1", "Tester", "hello")
        await _settle()

        assert "u1" not in a._pending_runs
        assert a.sent and "internal error" in a.sent[-1][1]

    asyncio.run(scenario())


def test_distinct_senders_are_independent(park):
    async def scenario():
        a = _adapter()
        await a._handle_inbound("u1", "One", "hi")    # u1 in flight
        await a._handle_inbound("u2", "Two", "hi")    # different sender: dispatches
        await _settle()
        assert a.forwards == 2                         # both dispatched
        assert a.sent == []
        await _finish_run(a, "u1")
        await _finish_run(a, "u2")

    asyncio.run(scenario())
