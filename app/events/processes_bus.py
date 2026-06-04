"""In-memory pub/sub bus for streaming process-list snapshots to UI subscribers.

Mirrors :class:`NotificationsStreamBus`: profile-keyed, threading-safe so the
exec-shell log-writer (which can publish from any context) doesn't have to
care which event loop a subscriber lives on. Snapshots are produced fresh
from ``list_processes(profile)`` at publish time, so the bus itself does not
need a ring buffer — late joiners get the current snapshot from the SSE
endpoint on connect.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class ProcessesStreamBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[str, List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}

    def subscribe(self, profile: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
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

    def publish(self, profile: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(profile, ()))
        for queue, loop in subs:
            loop.call_soon_threadsafe(queue.put_nowait, entry)


_instance: ProcessesStreamBus | None = None


def get_processes_stream_bus() -> ProcessesStreamBus:
    global _instance
    if _instance is None:
        _instance = ProcessesStreamBus()
    return _instance
