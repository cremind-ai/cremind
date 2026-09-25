"""The progress bus: per-profile, cross-thread, and latest-value only.

A large first sync publishes several snapshots a second. Every snapshot is
complete, so a subscriber that falls behind should skip to the newest one —
an unbounded queue would let one stalled browser tab hold thousands of them.
"""

from __future__ import annotations

import asyncio
import threading

from app.events.userdocs_bus import QUEUE_SIZE, UserDocsStreamBus


def test_a_slow_subscriber_keeps_only_the_newest_frames():
    async def run():
        bus = UserDocsStreamBus()
        q = bus.subscribe("alice")
        for i in range(QUEUE_SIZE * 3):
            bus.publish("alice", {"seq": i})
        await asyncio.sleep(0)  # let call_soon_threadsafe callbacks run
        got = []
        while not q.empty():
            got.append(q.get_nowait()["seq"])
        return got

    got = asyncio.run(run())
    assert len(got) == QUEUE_SIZE
    assert got[-1] == QUEUE_SIZE * 3 - 1
    assert got == sorted(got)


def test_profiles_never_see_each_others_frames():
    async def run():
        bus = UserDocsStreamBus()
        a = bus.subscribe("alice")
        b = bus.subscribe("bob")
        bus.publish("alice", {"who": "alice"})
        await asyncio.sleep(0)
        return a.qsize(), b.qsize()

    assert asyncio.run(run()) == (1, 0)


def test_worker_threads_can_publish():
    async def run():
        bus = UserDocsStreamBus()
        q = bus.subscribe("alice")
        t = threading.Thread(target=lambda: bus.publish("alice", {"from": "thread"}))
        t.start()
        t.join()
        return await asyncio.wait_for(q.get(), timeout=2)

    assert asyncio.run(run()) == {"from": "thread"}


def test_unsubscribe_stops_delivery():
    async def run():
        bus = UserDocsStreamBus()
        q = bus.subscribe("alice")
        bus.unsubscribe("alice", q)
        bus.publish("alice", {"x": 1})
        await asyncio.sleep(0)
        return q.qsize(), bus.has_subscribers("alice")

    assert asyncio.run(run()) == (0, False)
