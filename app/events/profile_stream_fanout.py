"""Profile-scoped fanout of per-conversation stream events.

:class:`ConversationStreamBus` is keyed by ``conversation_id``; each SSE
subscriber holds one HTTP/1.1 connection per conversation. With multiple
browser tabs viewing different conversations, that saturates Chrome's
6-per-host cap and stalls subsequent requests with "Provisional headers
are shown".

This fanout mirrors every ``ConversationStreamBus.publish()`` into a
per-profile queue, wrapping the event with ``conversation_id`` so the
profile-events SSE can demultiplex on the client side. One SSE per
profile carries events for **every** conversation that profile owns;
each tab subscribes once and filters by ``conversation_id`` locally.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List


class ProfileStreamFanoutBus:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: Dict[str, List[asyncio.Queue]] = {}

    async def subscribe(self, profile: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subs.setdefault(profile, []).append(queue)
        return queue

    async def unsubscribe(self, profile: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            bucket = self._subs.get(profile)
            if not bucket:
                return
            try:
                bucket.remove(queue)
            except ValueError:
                pass
            if not bucket:
                del self._subs[profile]

    async def publish(
        self, profile: str, conversation_id: str, event: Dict[str, Any],
    ) -> None:
        """Fan an event out to all profile subscribers.

        ``event`` is the same shape ConversationStreamBus.publish builds:
        ``{seq, type, data}``. We prepend ``conversation_id`` so the
        profile-events SSE consumer can dispatch by id.
        """
        async with self._lock:
            queues = list(self._subs.get(profile, ()))
        if not queues:
            return
        envelope = {"conversation_id": conversation_id, **event}
        for q in queues:
            q.put_nowait(envelope)


_instance: ProfileStreamFanoutBus | None = None


def get_profile_stream_fanout() -> ProfileStreamFanoutBus:
    global _instance
    if _instance is None:
        _instance = ProfileStreamFanoutBus()
    return _instance
