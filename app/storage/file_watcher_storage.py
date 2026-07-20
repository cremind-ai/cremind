"""Sync storage for conversation-scoped file watcher subscriptions.

Schema lives in :class:`app.storage.models.FileWatcherSubscriptionModel`. The
table is created by :class:`ConversationStorage.initialize`; this class only
reads/writes it. Backend chosen by the active
:class:`app.databases.DatabaseProvider`.

Mirrors :class:`EventSubscriptionStorage` so the watchdog manager can apply
the same pattern (idempotent ``arm``, distinct watch keys at boot,
``release_watcher`` after the last subscription on a folder is deleted).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase


class FileWatcherSubscriptionStorage(SyncStorageBase):
    """Sync storage for file_watcher_subscriptions."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "profile": row["profile"],
            "name": row["name"],
            "root_path": row["root_path"],
            "recursive": bool(row["recursive"]),
            "target_kind": row["target_kind"],
            "event_types": row["event_types"],
            "extensions": row["extensions"] or "",
            "action": row["action"],
            "created_at": row["created_at"],
            "paused": bool(row["paused"]),
        }

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM file_watcher_subscriptions WHERE id = :id"),
                {"id": id},
            ).mappings().fetchone()
            return self._row_to_dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM file_watcher_subscriptions ORDER BY created_at DESC")
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_profile(self, profile: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM file_watcher_subscriptions "
                    "WHERE profile = :profile ORDER BY created_at DESC"
                ),
                {"profile": profile},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM file_watcher_subscriptions "
                    "WHERE conversation_id = :conversation_id ORDER BY created_at DESC"
                ),
                {"conversation_id": conversation_id},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_by_root(
        self, profile: str, root_path: str, recursive: bool,
    ) -> List[Dict[str, Any]]:
        # ASC ordering matches the registration-order contract used by the
        # skill_events queue worker; multiple subs on the same root run
        # sequentially in created_at order.
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM file_watcher_subscriptions "
                    "WHERE profile = :profile AND root_path = :root_path AND recursive = :recursive "
                    "ORDER BY created_at ASC"
                ),
                {"profile": profile, "root_path": root_path, "recursive": bool(recursive)},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def insert(
        self,
        *,
        conversation_id: str,
        profile: str,
        name: str,
        root_path: str,
        recursive: bool,
        target_kind: str,
        event_types: str,
        extensions: str,
        action: str,
    ) -> Dict[str, Any]:
        new_id = str(uuid.uuid4())
        now = time.time()
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO file_watcher_subscriptions "
                    "(id, conversation_id, profile, name, root_path, recursive, "
                    "target_kind, event_types, extensions, action, created_at, paused) "
                    "VALUES (:id, :conversation_id, :profile, :name, :root_path, :recursive, "
                    ":target_kind, :event_types, :extensions, :action, :created_at, :paused)"
                ),
                {
                    "id": new_id, "conversation_id": conversation_id, "profile": profile,
                    "name": name, "root_path": root_path,
                    "recursive": bool(recursive), "target_kind": target_kind,
                    "event_types": event_types, "extensions": extensions or None,
                    "action": action, "created_at": now, "paused": False,
                },
            )
        return {
            "id": new_id,
            "conversation_id": conversation_id,
            "profile": profile,
            "name": name,
            "root_path": root_path,
            "recursive": bool(recursive),
            "target_kind": target_kind,
            "event_types": event_types,
            "extensions": extensions or "",
            "action": action,
            "created_at": now,
            "paused": False,
        }

    # Columns a caller may edit (manual Events-page / CLI edits). Excludes
    # identity/bookkeeping columns (id, conversation_id, profile, created_at).
    _EDITABLE = {
        "name", "root_path", "recursive", "target_kind", "event_types",
        "extensions", "action", "paused",
    }

    def update_fields(self, id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Patch editable columns. Returns the refreshed row (or None if absent).

        There is no ``updated_at`` column on this table, so nothing is bumped.
        Mirrors ``insert`` normalization: ``recursive`` → bool, empty
        ``extensions`` → NULL.
        """
        sets = {k: v for k, v in fields.items() if k in self._EDITABLE}
        if not sets:
            return self.get(id)
        if "recursive" in sets:
            sets["recursive"] = bool(sets["recursive"])
        if "extensions" in sets:
            sets["extensions"] = sets["extensions"] or None
        assignments = ", ".join(f"{k} = :{k}" for k in sets)
        params = dict(sets)
        params["id"] = id
        with self._engine.begin() as conn:
            conn.execute(
                text(f"UPDATE file_watcher_subscriptions SET {assignments} WHERE id = :id"),
                params,
            )
        return self.get(id)

    def delete(self, id: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("DELETE FROM file_watcher_subscriptions WHERE id = :id"),
                {"id": id},
            )
            return cur.rowcount > 0

    def distinct_watch_keys(self) -> List[Dict[str, Any]]:
        """Return one row per (profile, root_path, recursive) with subscriptions.

        Used at server boot to arm watchdogs and on subscription delete to
        decide whether the corresponding observer can be torn down.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT profile, root_path, recursive "
                    "FROM file_watcher_subscriptions"
                )
            ).mappings().fetchall()
            return [
                {
                    "profile": r["profile"],
                    "root_path": r["root_path"],
                    "recursive": bool(r["recursive"]),
                }
                for r in rows
            ]
