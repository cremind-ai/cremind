"""Sync storage for conversation-scoped schedule-event subscriptions.

Schema lives in :class:`app.storage.models.ScheduleEventSubscriptionModel`. The
table is created by the Alembic migrations run from
:meth:`ConversationStorage.initialize`; this class only reads/writes it. Backend
chosen by the active :class:`app.databases.DatabaseProvider`.

Mirrors :class:`FileWatcherSubscriptionStorage` so the time-driven
:class:`app.events.schedule_manager.ScheduleManager` can apply the same pattern
(load active rows at boot, mutate the rolling ``next_fire_at`` pointer, complete
or delete rows). A row is ONE rule; occurrences are never persisted.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase


class ScheduleEventSubscriptionStorage(SyncStorageBase):
    """Sync storage for schedule_event_subscriptions."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "profile": row["profile"],
            "title": row["title"] or "",
            "action": row["action"] or "",
            "is_reminder_only": bool(row["is_reminder_only"]),
            "all_day": bool(row["all_day"]),
            "schedule_kind": row["schedule_kind"],
            "dtstart": row["dtstart"],
            "duration_minutes": int(row["duration_minutes"]),
            "rrule": row["rrule"],
            "recurrence_end_type": row["recurrence_end_type"],
            "recurrence_end_value": row["recurrence_end_value"],
            "timezone": row["timezone"],
            "next_fire_at": row["next_fire_at"],
            "occurrences_fired": int(row["occurrences_fired"]),
            "status": row["status"],
            "source": row["source"],
            "external_provider": row["external_provider"],
            "external_event_id": row["external_event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM schedule_event_subscriptions WHERE id = :id"),
                {"id": id},
            ).mappings().fetchone()
            return self._row_to_dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM schedule_event_subscriptions ORDER BY created_at DESC")
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_active(self) -> List[Dict[str, Any]]:
        """Rows the ScheduleManager should arm: active with a pending fire.

        Ordered by ``next_fire_at`` so the manager can seed its heap cheaply.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM schedule_event_subscriptions "
                    "WHERE status = 'active' AND next_fire_at IS NOT NULL "
                    "ORDER BY next_fire_at ASC"
                )
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_profile(self, profile: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM schedule_event_subscriptions "
                    "WHERE profile = :profile ORDER BY dtstart ASC"
                ),
                {"profile": profile},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM schedule_event_subscriptions "
                    "WHERE conversation_id = :conversation_id ORDER BY dtstart ASC"
                ),
                {"conversation_id": conversation_id},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def insert(
        self,
        *,
        conversation_id: str,
        profile: str,
        title: str,
        action: str,
        is_reminder_only: bool,
        schedule_kind: str,
        dtstart: str,
        duration_minutes: int,
        next_fire_at: Optional[float],
        all_day: bool = False,
        rrule: Optional[str] = None,
        recurrence_end_type: Optional[str] = None,
        recurrence_end_value: Optional[str] = None,
        timezone: Optional[str] = None,
        status: str = "active",
        source: str = "agent",
        external_provider: Optional[str] = None,
        external_event_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        new_id = str(uuid.uuid4())
        now = time.time()
        params = {
            "id": new_id,
            "conversation_id": conversation_id,
            "profile": profile,
            "title": title or "",
            "action": action or "",
            "is_reminder_only": bool(is_reminder_only),
            "all_day": bool(all_day),
            "schedule_kind": schedule_kind,
            "dtstart": dtstart,
            "duration_minutes": int(duration_minutes),
            "rrule": rrule,
            "recurrence_end_type": recurrence_end_type,
            "recurrence_end_value": recurrence_end_value,
            "timezone": timezone,
            "next_fire_at": next_fire_at,
            "occurrences_fired": 0,
            "status": status,
            "source": source,
            "external_provider": external_provider,
            "external_event_id": external_event_id,
            "created_at": now,
            "updated_at": now,
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO schedule_event_subscriptions "
                    "(id, conversation_id, profile, title, action, is_reminder_only, all_day, "
                    "schedule_kind, dtstart, duration_minutes, rrule, recurrence_end_type, "
                    "recurrence_end_value, timezone, next_fire_at, occurrences_fired, "
                    "status, source, external_provider, external_event_id, created_at, updated_at) "
                    "VALUES (:id, :conversation_id, :profile, :title, :action, :is_reminder_only, :all_day, "
                    ":schedule_kind, :dtstart, :duration_minutes, :rrule, :recurrence_end_type, "
                    ":recurrence_end_value, :timezone, :next_fire_at, :occurrences_fired, "
                    ":status, :source, :external_provider, :external_event_id, :created_at, :updated_at)"
                ),
                params,
            )
        return params

    def update_next_fire(
        self, id: str, *, next_fire_at: Optional[float], occurrences_fired: int,
    ) -> None:
        """Advance the rolling pointer after a fire (or clear it on completion)."""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE schedule_event_subscriptions "
                    "SET next_fire_at = :n, occurrences_fired = :o, updated_at = :u "
                    "WHERE id = :id"
                ),
                {"n": next_fire_at, "o": int(occurrences_fired), "u": time.time(), "id": id},
            )

    def set_status(self, id: str, status: str, *, next_fire_at: Optional[float] = None) -> None:
        """Flip status (active|completed|cancelled|paused), optionally re-seeding
        the pointer (e.g. on resume)."""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE schedule_event_subscriptions "
                    "SET status = :s, next_fire_at = :n, updated_at = :u WHERE id = :id"
                ),
                {"s": status, "n": next_fire_at, "u": time.time(), "id": id},
            )

    # Columns a caller may edit (manual calendar edits / agent updates). Excludes
    # identity/bookkeeping columns.
    _EDITABLE = {
        "title", "action", "is_reminder_only", "all_day", "schedule_kind", "dtstart",
        "duration_minutes", "rrule", "recurrence_end_type", "recurrence_end_value",
        "timezone", "next_fire_at", "status", "external_provider", "external_event_id",
    }

    def update_fields(self, id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Patch arbitrary editable columns + bump ``updated_at``. Returns the row."""
        sets = {k: v for k, v in fields.items() if k in self._EDITABLE}
        if not sets:
            return self.get(id)
        sets["updated_at"] = time.time()
        assignments = ", ".join(f"{k} = :{k}" for k in sets)
        params = dict(sets)
        params["id"] = id
        with self._engine.begin() as conn:
            conn.execute(
                text(f"UPDATE schedule_event_subscriptions SET {assignments} WHERE id = :id"),
                params,
            )
        return self.get(id)

    def delete(self, id: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("DELETE FROM schedule_event_subscriptions WHERE id = :id"),
                {"id": id},
            )
            return cur.rowcount > 0
