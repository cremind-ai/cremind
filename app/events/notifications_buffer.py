"""In-memory ring buffer for event-driven completion notifications.

The frontend's notifications store is client-side ``localStorage``. The buffer
holds the last N entries per profile so that a UI client opening fresh can
catch up on recent server-fired notifications via the SSE replay snapshot.
Live delivery happens through :class:`NotificationsStreamBus`.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Literal

from app.events.notifications_bus import get_notifications_stream_bus


_MAX_PER_PROFILE = 100

Priority = Literal["high", "normal"]


class EventNotificationsBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_profile: Dict[str, List[Dict[str, Any]]] = {}

    def push(
        self,
        *,
        profile: str,
        conversation_id: str,
        conversation_title: str,
        message_preview: str,
        kind: str = "completed",
        priority: Priority = "normal",
        extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        entry = {
            "id": str(uuid.uuid4()),
            "profile": profile,
            "conversation_id": conversation_id,
            "conversation_title": conversation_title,
            "message_preview": message_preview,
            "kind": kind,
            "priority": priority,
            "created_at": time.time() * 1000,
        }
        if extra:
            entry.update(extra)
        with self._lock:
            bucket = self._by_profile.setdefault(profile, [])
            bucket.append(entry)
            if len(bucket) > _MAX_PER_PROFILE:
                del bucket[: len(bucket) - _MAX_PER_PROFILE]
        get_notifications_stream_bus().publish(profile, entry)
        _journal_for_tags(profile, entry)
        return entry

    def since(self, profile: str, since_ms: float) -> List[Dict[str, Any]]:
        with self._lock:
            bucket = self._by_profile.get(profile, [])
            return [e for e in bucket if e["created_at"] > since_ms]


def _journal_for_tags(profile: str, entry: Dict[str, Any]) -> None:
    """Cremind Tag: hand a copy to the journal (asynchronously — ``push`` stays
    synchronous; the journal commit is the acceptance boundary). An OTP, and
    any kind the journal records on its own, is dropped by the builder."""
    try:
        from app.tags import journal
        from app.tags.sanitize import notification_entry

        journal_entry = notification_entry(entry)
        if journal_entry is not None:
            journal.submit_standalone(profile, [journal_entry])
    except Exception:  # noqa: BLE001 — a notification must never fail over this
        pass


_instance: EventNotificationsBuffer | None = None


def get_event_notifications() -> EventNotificationsBuffer:
    global _instance
    if _instance is None:
        _instance = EventNotificationsBuffer()
    return _instance
