"""In-memory pub/sub bus signalling event-run state changes to the Events page.

Mirrors :class:`app.events.schedule_events_admin_bus.ScheduleEventsAdminStreamBus`:
profile-keyed, threading-safe so the (async) run dispatcher / stream runner and
the API handlers can all publish. The payload is a sentinel — the SSE endpoint
rebuilds the actual run snapshot on each tick.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class EventRunsAdminStreamBus:
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
            try:
                loop.call_soon_threadsafe(queue.put_nowait, entry)
            except RuntimeError:
                pass


_instance: EventRunsAdminStreamBus | None = None


def get_event_runs_admin_stream_bus() -> EventRunsAdminStreamBus:
    global _instance
    if _instance is None:
        _instance = EventRunsAdminStreamBus()
    return _instance


def publish_event_runs_changed(profile: str) -> None:
    """Signal that a run for ``profile`` changed (created / status / finished)."""
    if not profile:
        return
    get_event_runs_admin_stream_bus().publish(profile, {})
