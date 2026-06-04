"""In-memory pub/sub bus for streaming event-driven notifications to UI subscribers.

Mirrors :class:`ConversationStreamBus` but is keyed by **profile** rather than
conversation, since notifications are profile-scoped (one user may have many
conversations producing events). Replay snapshots are read from
:class:`EventNotificationsBuffer`, which already keeps the last N entries per
profile, so the bus itself does not need its own ring buffer.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class NotificationsStreamBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # profile -> list of (queue, loop) pairs. The loop is captured at
        # subscribe-time so publish() can be called from any thread or sync
        # context (push() is sync) and still hand work back to the right loop.
        self._subs: Dict[str, List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}

    def subscribe(self, profile: str) -> asyncio.Queue:
        """Register a subscriber on the running event loop and return its queue."""
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
        """Fan an entry out to all live subscribers for the given profile.

        Safe to call from sync code, async code, or a non-loop thread:
        each subscriber's queue is fed via ``loop.call_soon_threadsafe``.
        """
        with self._lock:
            subs = list(self._subs.get(profile, ()))
        try:
            from app.utils.logger import logger
            logger.info(
                f"[debug:notif-bus] publish profile={profile} kind={entry.get('kind')} "
                f"conversation_id={entry.get('conversation_id')} subscribers={len(subs)}"
            )
        except Exception:  # noqa: BLE001
            pass
        for queue, loop in subs:
            loop.call_soon_threadsafe(queue.put_nowait, entry)


_instance: NotificationsStreamBus | None = None


def get_notifications_stream_bus() -> NotificationsStreamBus:
    global _instance
    if _instance is None:
        _instance = NotificationsStreamBus()
    return _instance
