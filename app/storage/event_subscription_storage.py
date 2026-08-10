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

from sqlalchemy import bindparam, text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase
from app.storage._task_subscription import TaskSubscriptionMixin


class EventSubscriptionStorage(TaskSubscriptionMixin, SyncStorageBase):
    """Sync storage for skill_event_subscriptions."""

    _TASK_TABLE = "skill_event_subscriptions"

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
            "task": bool(row["task"]),
            "task_status": row["task_status"],
            "timeout_at": row["timeout_at"],
            "completed_at": row["completed_at"],
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
        task: bool = False,
        timeout_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Append a new subscription row.

        Multiple subscriptions for the same (conversation, skill, event_type)
        are allowed and will run sequentially in created_at order when the
        event fires.

        ``task=True`` makes this an EVENT TASK — a one-shot whose run result is
        delivered back into ``conversation_id``; it starts at
        ``task_status='active'`` and is consumed by :meth:`claim_task_fire`.
        ``timeout_at`` (epoch seconds) is only meaningful for a task.
        """
        new_id = str(uuid.uuid4())
        now = time.time()
        task = bool(task)
        task_status = "active" if task else None
        timeout_at = float(timeout_at) if (task and timeout_at is not None) else None
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO skill_event_subscriptions "
                    "(id, conversation_id, profile, skill_name, event_type, action, created_at, "
                    "paused, task, task_status, timeout_at, completed_at) "
                    "VALUES (:id, :conversation_id, :profile, :skill_name, :event_type, :action, "
                    ":created_at, :paused, :task, :task_status, :timeout_at, NULL)"
                ),
                {
                    "id": new_id, "conversation_id": conversation_id, "profile": profile,
                    "skill_name": skill_name, "event_type": event_type, "action": action,
                    "created_at": now, "paused": False,
                    "task": task, "task_status": task_status, "timeout_at": timeout_at,
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
            "task": task,
            "task_status": task_status,
            "timeout_at": timeout_at,
            "completed_at": None,
        }

    # Columns a caller may edit (manual Events-page / CLI edits). skill_name is
    # not editable (the subscription is pinned to its skill); identity and
    # created_at are excluded. ``task``/``task_status``/``completed_at`` are
    # excluded too: task-ness is immutable, and the lifecycle columns are only
    # ever moved by the atomic claim helpers below (never by a PATCH).
    _EDITABLE = {"event_type", "action", "paused", "timeout_at"}

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

    def repoint_skill(
        self,
        *,
        profile: str,
        old_skill_names: List[str],
        new_skill_name: str,
        event_type: Optional[str] = None,
        pause: bool = True,
    ) -> List[str]:
        """Boot-time repair: move subscriptions from a retired skill to its successor.

        Deliberately bypasses :attr:`_EDITABLE` — ``skill_name`` is pinned for
        user-facing edits precisely so a subscription cannot drift off its skill,
        but when a *builtin* stops declaring an event its rows are orphaned: they
        list as active forever and can never fire. Only a direct rewrite can
        rescue them, so this method exists for that one caller
        (``app.skills.sync``) and is not exposed through the API.

        Pausing is the point, not a side effect: the successor skill usually needs
        setup before its events flow, so the row is preserved (action text intact)
        and left for the user to resume rather than silently re-armed.

        Returns the ids of the rows rewritten — empty when there is nothing to do,
        which is what makes a repeated boot a no-op.
        """
        if not old_skill_names:
            return []
        select_sql = (
            "SELECT id FROM skill_event_subscriptions "
            "WHERE profile = :profile AND skill_name IN :names"
        )
        params: Dict[str, Any] = {"profile": profile, "names": list(old_skill_names)}
        if event_type is not None:
            select_sql += " AND event_type = :event_type"
            params["event_type"] = event_type
        select_stmt = text(select_sql).bindparams(bindparam("names", expanding=True))

        with self._engine.begin() as conn:
            ids = [r["id"] for r in conn.execute(select_stmt, params).mappings().fetchall()]
            if not ids:
                return []
            update_stmt = text(
                "UPDATE skill_event_subscriptions "
                "SET skill_name = :new_skill_name, paused = :paused WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True))
            conn.execute(
                update_stmt,
                {"new_skill_name": new_skill_name, "paused": bool(pause), "ids": ids},
            )
        return ids

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
