"""In-memory pub/sub bus signalling conversations-list state changes.

Mirrors :class:`SkillEventsAdminStreamBus`: profile-keyed, threading-safe so
any async or sync caller can publish.

Wakeup-only — building the actual snapshot (per-profile conversation list
filtered by channel) requires async DB calls, so it lives in the SSE
endpoint generator. Subscribers wake on each tick and rebuild the snapshot
before yielding the next frame.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class ConversationsListStreamBus:
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


_instance: ConversationsListStreamBus | None = None


def get_conversations_list_stream_bus() -> ConversationsListStreamBus:
    global _instance
    if _instance is None:
        _instance = ConversationsListStreamBus()
    return _instance


def publish_conversations_changed(profile: str | None) -> None:
    """Tell the conversations-list SSE endpoint to rebuild and push a snapshot.

    Wakeup-only — subscribers rebuild the snapshot themselves.
    """
    if not profile:
        return
    try:
        get_conversations_list_stream_bus().publish(profile, {})
    except Exception:  # noqa: BLE001
        from app.utils.logger import logger
        logger.debug("conversations-list bus publish failed", exc_info=True)
