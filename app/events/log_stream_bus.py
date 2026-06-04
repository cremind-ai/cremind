"""In-memory pub/sub bus for the server-wide log stream (SSE).

A single process-wide topic — every subscriber sees every log record.
Loguru sinks publish here; the ``/api/server/logs/stream`` endpoint
subscribes and fans records out to the Developer page in the UI.

A small ring buffer holds the most recent records so that a freshly
connected client gets immediate context before the live tail begins.

Cross-thread safety: publishes arrive from whichever thread Loguru
hands the record to (sync logger calls run on the caller's thread,
``enqueue=True`` sinks run on Loguru's worker — though this bus sink
intentionally does NOT use ``enqueue``). Subscriber queues belong to
the main event loop, so we hop via ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Tuple


_RING_CAP = 500
_QUEUE_CAP = 2000


class LogStreamBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ring: Deque[Dict[str, Any]] = deque(maxlen=_RING_CAP)
        self._subs: List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []

    def subscribe(self) -> Tuple[asyncio.Queue, List[Dict[str, Any]]]:
        """Register a subscriber and atomically snapshot the ring buffer.

        Returns ``(queue, backfill)``. Holding the lock across both the
        snapshot and the registration is the race-free contract: any
        record published concurrently either lands in ``backfill`` (and
        not in ``queue``) or in ``queue`` (and not in ``backfill``) —
        never both, never neither.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_CAP)
        loop = asyncio.get_running_loop()
        with self._lock:
            backfill = list(self._ring)
            self._subs.append((queue, loop))
        return queue, backfill

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(q, l) for (q, l) in self._subs if q is not queue]

    def publish(self, entry: Dict[str, Any]) -> None:
        """Append to the ring buffer and fan out to every subscriber.

        Safe to call from any thread. Failures are swallowed — a broken
        SSE pipe must never break the ``logger.info`` call site that
        triggered the publish.
        """
        with self._lock:
            self._ring.append(entry)
            subs = list(self._subs)
        for queue, loop in subs:
            try:
                loop.call_soon_threadsafe(_offer, queue, entry)
            except RuntimeError:
                # Loop is closed (subscriber went away mid-publish).
                # The disconnect handler will unsubscribe shortly.
                pass


def _offer(queue: asyncio.Queue, entry: Dict[str, Any]) -> None:
    """Best-effort put_nowait. Drop on QueueFull — debug tool, not audit log."""
    try:
        queue.put_nowait(entry)
    except asyncio.QueueFull:
        pass


_instance: LogStreamBus | None = None


def get_log_stream_bus() -> LogStreamBus:
    global _instance
    if _instance is None:
        _instance = LogStreamBus()
    return _instance
