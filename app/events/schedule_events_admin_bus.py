"""In-memory pub/sub bus signalling schedule-events admin-page state changes.

Mirrors :class:`app.events.skill_events_admin_bus.SkillEventsAdminStreamBus`:
profile-keyed, threading-safe so the (threaded) ScheduleManager and the (async)
API handlers can both publish. The payload is a sentinel — the SSE endpoint
rebuilds the actual subscription snapshot on each tick.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class ScheduleEventsAdminStreamBus:
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


_instance: ScheduleEventsAdminStreamBus | None = None


def get_schedule_events_admin_stream_bus() -> ScheduleEventsAdminStreamBus:
    global _instance
    if _instance is None:
        _instance = ScheduleEventsAdminStreamBus()
    return _instance
