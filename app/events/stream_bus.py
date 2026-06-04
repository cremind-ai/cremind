"""In-memory pub/sub bus for streaming agent runs to UI subscribers.

Used by the event runner to broadcast each chunk of an event-triggered agent
run (thinking steps, observation results, text tokens, files, terminals,
token usage, summary, completion) to any UI client currently watching the
conversation. The SSE endpoint at ``/api/conversations/{id}/stream`` opens a
subscriber queue against this bus.

A small per-conversation ring buffer holds the events of the *current* run so
that a UI client opening the conversation mid-stream can replay everything
from the start of the run before the live tail begins.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

from app.events.profile_stream_fanout import get_profile_stream_fanout


_RING_CAP = 500


class ConversationStreamBus:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Per-conversation state. We track:
        #   subscribers — list of asyncio.Queue
        #   ring        — list of events emitted in the current run (replay)
        #   seq         — monotonic seq counter for the current run
        #   active      — True between start_run and the next start_run/clear
        #   profile     — owning profile for each conversation, captured on
        #                 start_run; used to fan events to the profile-scoped
        #                 stream so the UI doesn't need a per-conversation
        #                 SSE connection.
        self._subs: Dict[str, List[asyncio.Queue]] = {}
        self._ring: Dict[str, List[Dict[str, Any]]] = {}
        self._seq: Dict[str, int] = {}
        self._active: Dict[str, bool] = {}
        self._profile: Dict[str, str] = {}

    def is_active(self, conversation_id: str) -> bool:
        return bool(self._active.get(conversation_id))

    async def start_run(self, conversation_id: str, profile: str) -> None:
        """Mark a new run as starting; clear the prior run's replay buffer.

        The ``seq`` counter is **not** reset — it stays monotonic for the
        lifetime of the conversation. Subscribers dedupe by ``seq`` across
        SSE reconnects, so reusing seqs across runs would silently swallow
        the second-and-later runs' events.

        ``profile`` is recorded so subsequent ``publish`` calls can fan
        the event to the per-profile stream as well as the per-conversation
        subscribers.
        """
        async with self._lock:
            self._ring[conversation_id] = []
            self._active[conversation_id] = True
            self._profile[conversation_id] = profile

    async def end_run(self, conversation_id: str) -> None:
        """Mark the current run as ended and clear the replay buffer.

        Once a run finishes, its events have been persisted to SQLite and any
        new UI client opening the conversation will get them via the regular
        ``fetchConversationMessages`` path. Keeping stale events in the ring
        would cause a fresh subscriber to render the run a second time on top
        of the persisted version, producing duplicate message bubbles.
        """
        async with self._lock:
            self._active[conversation_id] = False
            self._ring[conversation_id] = []

    async def publish(
        self, conversation_id: str, event_type: str, data: Any,
    ) -> Dict[str, Any]:
        """Append an event to the ring buffer and fan it out to subscribers."""
        async with self._lock:
            seq = self._seq.get(conversation_id, 0) + 1
            self._seq[conversation_id] = seq
            event = {"seq": seq, "type": event_type, "data": data}
            ring = self._ring.setdefault(conversation_id, [])
            ring.append(event)
            if len(ring) > _RING_CAP:
                del ring[: len(ring) - _RING_CAP]
            queues = list(self._subs.get(conversation_id, ()))
            profile = self._profile.get(conversation_id)
        for q in queues:
            # put_nowait is safe — queues are unbounded.
            q.put_nowait(event)
        if profile is not None:
            await get_profile_stream_fanout().publish(profile, conversation_id, event)
        return event

    async def subscribe(
        self, conversation_id: str,
    ) -> Tuple[asyncio.Queue, List[Dict[str, Any]], bool]:
        """Register a subscriber and return its queue plus replay snapshot."""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subs.setdefault(conversation_id, []).append(queue)
            replay = list(self._ring.get(conversation_id, ()))
            is_active = bool(self._active.get(conversation_id))
        return queue, replay, is_active

    async def unsubscribe(
        self, conversation_id: str, queue: asyncio.Queue,
    ) -> None:
        async with self._lock:
            bucket = self._subs.get(conversation_id)
            if not bucket:
                return
            try:
                bucket.remove(queue)
            except ValueError:
                pass
            if not bucket:
                del self._subs[conversation_id]

    async def discard(self, conversation_id: str) -> None:
        """Drop all in-memory state for a conversation id.

        Called when a conversation is renamed or deleted. ``_seq`` survives
        across runs (kept monotonic so SSE-reconnect dedupe works), so a
        rename without discarding would leak the old id's counter forever.
        Subscribers are expected to be empty when the rename is allowed
        (the API rejects rename while ``is_active`` is true).
        """
        async with self._lock:
            self._subs.pop(conversation_id, None)
            self._ring.pop(conversation_id, None)
            self._seq.pop(conversation_id, None)
            self._active.pop(conversation_id, None)
            self._profile.pop(conversation_id, None)

    async def snapshot_for_profile(
        self, profile: str,
    ) -> List[Tuple[str, List[Dict[str, Any]]]]:
        """Return ``(conversation_id, ring)`` for every active conversation
        of ``profile``.

        Used by the profile-events SSE on connect to replay in-flight runs
        to a late subscriber, matching the per-conversation endpoint's
        replay-on-subscribe contract.
        """
        async with self._lock:
            out: List[Tuple[str, List[Dict[str, Any]]]] = []
            for conv_id, conv_profile in self._profile.items():
                if conv_profile != profile:
                    continue
                if not self._active.get(conv_id):
                    continue
                out.append((conv_id, list(self._ring.get(conv_id, ()))))
            return out


_instance: ConversationStreamBus | None = None


def get_event_stream_bus() -> ConversationStreamBus:
    global _instance
    if _instance is None:
        _instance = ConversationStreamBus()
    return _instance
