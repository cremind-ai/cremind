"""In-memory pub/sub bus for User Document Search sync-progress snapshots.

Profile-keyed and thread-safe like :mod:`app.events.processes_bus` — the sync
engine publishes from worker threads — with one difference that matters: each
subscriber's queue is **bounded and drops the oldest frame** when full.

Every frame is a complete snapshot (see ``app.userdocs`` progress), so a
subscriber that falls behind loses nothing by skipping to the newest one. An
unbounded queue, which is fine for the processes list, would let one slow
browser tab accumulate thousands of progress frames during a large first sync.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple

# Enough to ride out a burst of state transitions without dropping the one a
# client cares about, small enough that a stalled tab holds kilobytes, not MBs.
QUEUE_SIZE = 8


def _offer(queue: asyncio.Queue, item: Dict[str, Any]) -> None:
    """Put ``item``, discarding the oldest frame if the queue is full.

    Runs on the subscriber's own loop (scheduled via ``call_soon_threadsafe``),
    so the get-then-put pair cannot interleave with the consumer.
    """
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


class UserDocsStreamBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[str, List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}

    def subscribe(self, profile: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subs.setdefault(profile, []).append((queue, loop))
        return queue

    def unsubscribe(self, profile: str, queue: asyncio.Queue) -> None:
        with self._lock:
            bucket = self._subs.get(profile)
            if not bucket:
                return
            self._subs[profile] = [(q, l) for (q, l) in bucket if q is not queue]
            if not self._subs[profile]:
                del self._subs[profile]

    def has_subscribers(self, profile: str) -> bool:
        with self._lock:
            return bool(self._subs.get(profile))

    def publish(self, profile: str, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(profile, ()))
        for queue, loop in subs:
            try:
                loop.call_soon_threadsafe(_offer, queue, snapshot)
            except RuntimeError:
                # The subscriber's loop has closed; its SSE generator's
                # ``finally`` will unsubscribe it.
                pass


_instance: UserDocsStreamBus | None = None


def get_userdocs_stream_bus() -> UserDocsStreamBus:
    global _instance
    if _instance is None:
        _instance = UserDocsStreamBus()
    return _instance


def publish_userdocs_changed(profile: str | None, snapshot: Dict[str, Any]) -> None:
    """Publish a snapshot for ``profile``. Never raises; ignores a falsy profile."""
    if not profile:
        return
    try:
        get_userdocs_stream_bus().publish(profile, snapshot)
    except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
        pass
