"""Sync storage for conversation-scoped skill event subscriptions.

Schema lives in :class:`app.storage.models.SkillEventSubscriptionModel`. The
table is created by :class:`ConversationStorage.initialize`; this class only
reads/writes it. Backend chosen by the active
:class:`app.databases.DatabaseProvider`.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase


class EventSubscriptionStorage(SyncStorageBase):
    """Sync storage for skill_event_subscriptions."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "profile": row["profile"],
            "skill_name": row["skill_name"],
            "event_type": row["event_type"],
            "action": row["action"],
            "created_at": row["created_at"],
            "paused": bool(row["paused"]),
        }

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM skill_event_subscriptions WHERE id = :id"),
                {"id": id},
            ).mappings().fetchone()
            return self._row_to_dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM skill_event_subscriptions ORDER BY created_at DESC")
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_profile(self, profile: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM skill_event_subscriptions "
                    "WHERE profile = :profile ORDER BY created_at DESC"
                ),
                {"profile": profile},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM skill_event_subscriptions "
                    "WHERE conversation_id = :conversation_id ORDER BY created_at DESC"
                ),
                {"conversation_id": conversation_id},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_event(
        self, profile: str, skill_name: str, event_type: str,
    ) -> List[Dict[str, Any]]:
        # ASC ordering is the contract that lets the queue worker run
        # subscriptions in registration order — first registered, first run.
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM skill_event_subscriptions "
                    "WHERE profile = :profile AND skill_name = :skill_name AND event_type = :event_type "
                    "ORDER BY created_at ASC"
                ),
                {"profile": profile, "skill_name": skill_name, "event_type": event_type},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def insert(
        self,
        *,
        conversation_id: str,
        profile: str,
        skill_name: str,
        event_type: str,
        action: str,
    ) -> Dict[str, Any]:
        """Append a new subscription row.

        Multiple subscriptions for the same (conversation, skill, event_type)
        are allowed and will run sequentially in created_at order when the
        event fires.
        """
        new_id = str(uuid.uuid4())
        now = time.time()
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO skill_event_subscriptions "
                    "(id, conversation_id, profile, skill_name, event_type, action, created_at, paused) "
                    "VALUES (:id, :conversation_id, :profile, :skill_name, :event_type, :action, :created_at, :paused)"
                ),
                {
                    "id": new_id, "conversation_id": conversation_id, "profile": profile,
                    "skill_name": skill_name, "event_type": event_type, "action": action,
                    "created_at": now, "paused": False,
                },
            )
        return {
            "id": new_id,
            "conversation_id": conversation_id,
            "profile": profile,
            "skill_name": skill_name,
            "event_type": event_type,
            "action": action,
            "created_at": now,
            "paused": False,
        }

    # Columns a caller may edit (manual Events-page / CLI edits). skill_name is
    # not editable (the subscription is pinned to its skill); identity and
    # created_at are excluded.
    _EDITABLE = {"event_type", "action", "paused"}

    def update_fields(self, id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Patch editable columns. Returns the refreshed row (or None if absent).

        There is no ``updated_at`` column on this table, so nothing is bumped.
        """
        sets = {k: v for k, v in fields.items() if k in self._EDITABLE}
        if not sets:
            return self.get(id)
        assignments = ", ".join(f"{k} = :{k}" for k in sets)
        params = dict(sets)
        params["id"] = id
        with self._engine.begin() as conn:
            conn.execute(
                text(f"UPDATE skill_event_subscriptions SET {assignments} WHERE id = :id"),
                params,
            )
        return self.get(id)

    def delete(self, id: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("DELETE FROM skill_event_subscriptions WHERE id = :id"),
                {"id": id},
            )
            return cur.rowcount > 0

    def distinct_event_folders(self) -> List[Dict[str, Any]]:
        """Return one row per (profile, skill_name, event_type) with subscriptions.

        Used at server boot to arm watchdogs and on subscription delete to
        decide whether the corresponding watcher can be torn down.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT profile, skill_name, event_type "
                    "FROM skill_event_subscriptions"
                )
            ).mappings().fetchall()
            return [
                {
                    "profile": r["profile"],
                    "skill_name": r["skill_name"],
                    "event_type": r["event_type"],
                }
                for r in rows
            ]
