"""``BaseChannelAdapter.forget_sender`` — the in-memory half of deleting a client.

Deleting the rows is not enough to make "as if they had never messaged" true:
the adapter keeps per-sender state in memory, and each leftover has a visible
consequence. A stale busy flag swallows their next "I'm thinking…" ack; a
remembered access request means the operator never gets a fresh approval
notification when the person comes back.

The one thing that must NOT be reclaimed unconditionally is a *held* inbound
lock — the adapter's own comment explains that a task can be parked on it, so
dropping it would hand out a second lock and break the mutual exclusion.
"""

from __future__ import annotations

import asyncio

from app.channels.base import BaseChannelAdapter


class _Adapter(BaseChannelAdapter):
    def __init__(self) -> None:
        super().__init__(
            {"id": "ch1", "profile": "p1", "channel_type": "telegram", "mode": "bot"},
            object(),
        )
        self.sent: list[tuple[str, str]] = []

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append((sender_id, text))


def test_forget_clears_busy_and_access_memos():
    a = _Adapter()
    a._busy_acked.add("s1")
    a._access_requested.add("s1")

    a.forget_sender("s1")

    assert "s1" not in a._busy_acked
    assert "s1" not in a._access_requested


def test_forget_leaves_other_senders_alone():
    a = _Adapter()
    a._busy_acked.update({"s1", "s2"})
    a._access_requested.update({"s1", "s2"})

    a.forget_sender("s1")

    assert a._busy_acked == {"s2"}
    assert a._access_requested == {"s2"}


def test_forget_is_safe_for_an_unknown_sender():
    a = _Adapter()
    a.forget_sender("never-seen")  # must not raise


def test_forget_reclaims_an_idle_inbound_lock():
    a = _Adapter()
    a._inbound_lock("s1")
    assert "s1" in a._inbound_locks

    a.forget_sender("s1")

    assert "s1" not in a._inbound_locks


def test_forget_keeps_a_held_inbound_lock():
    """Reclaiming a held lock would hand the next caller a second one."""
    async def _scenario():
        a = _Adapter()
        lock = a._inbound_lock("s1")
        async with lock:
            a.forget_sender("s1")
            # Still registered, and still the same object the holder owns.
            assert a._inbound_locks.get("s1") is lock
        return a, lock

    a, lock = asyncio.run(_scenario())
    # Once released it is reclaimable again.
    a.forget_sender("s1")
    assert "s1" not in a._inbound_locks


def test_forget_cancels_an_inflight_forwarder():
    """A forwarder for a deleted conversation has nowhere to publish."""
    async def _scenario():
        a = _Adapter()
        started = asyncio.Event()

        async def _forever():
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        a._inflight["s1"] = task
        await started.wait()

        a.forget_sender("s1")
        assert "s1" not in a._inflight
        # Give the loop a tick to deliver the cancellation.
        await asyncio.sleep(0)
        assert task.cancelled() or task.cancelling()
        return True

    assert asyncio.run(_scenario()) is True
