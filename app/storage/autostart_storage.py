"""Sync storage for autostart process registrations.

The ``autostart_processes`` table is created by
:class:`app.storage.conversation_storage.ConversationStorage` (which owns all
``CREATE TABLE`` statements); this class only reads/writes it. Backend chosen
by the active :class:`app.databases.DatabaseProvider`.

Schema lives in :class:`app.storage.models.AutostartProcessModel`.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase
from app.utils.logger import logger


def _listener_name(working_dir: Optional[str]) -> str:
    """The skill a listener belongs to, from its working directory
    (``…/skills/<skill>/scripts`` → ``<skill>``)."""
    parts = [p for p in str(working_dir or "").replace("\\", "/").split("/") if p]
    if parts and parts[-1] == "scripts":
        parts = parts[:-1]
    return f"{parts[-1]} listener" if parts else "Autostart process"


class AutostartStorage(SyncStorageBase):
    """Sync storage for autostart process registrations."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "profile": row["profile"],
            "command": row["command"],
            "working_dir": row["working_dir"] or "",
            "is_pty": bool(row["is_pty"]),
            "created_at": row["created_at"],
            "last_error": row["last_error"],
            "last_attempted_at": row["last_attempted_at"],
        }

    def list(self, profile: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM autostart_processes WHERE profile = :profile ORDER BY created_at DESC"),
                {"profile": profile},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM autostart_processes ORDER BY created_at DESC")
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM autostart_processes WHERE id = :id"),
                {"id": id},
            ).mappings().fetchone()
            return self._row_to_dict(row) if row else None

    def find_duplicate(
        self, profile: str, command: str, *, working_dir: str
    ) -> Optional[Dict[str, Any]]:
        # Process identity is (profile, command, working_dir) — NOT command
        # alone. Many skills share the identical command string
        # (``uv run scripts/event_listener.py``); they are distinct apps
        # because they run from distinct skill directories. ``working_dir`` is
        # compared verbatim, exactly like ``command``: each skill is always
        # registered through one deterministic code path, so the stored string
        # is stable for a genuine re-registration (see the module's callers).
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM autostart_processes "
                    "WHERE profile = :profile AND command = :command "
                    "AND working_dir = :working_dir LIMIT 1"
                ),
                {
                    "profile": profile,
                    "command": command,
                    "working_dir": working_dir or "",
                },
            ).mappings().fetchone()
            return self._row_to_dict(row) if row else None

    def insert(
        self,
        *,
        profile: str,
        command: str,
        working_dir: str,
        is_pty: bool,
    ) -> Dict[str, Any]:
        row = {
            "id": str(uuid.uuid4()),
            "profile": profile,
            "command": command,
            "working_dir": working_dir or "",
            "is_pty": bool(is_pty),
            "created_at": time.time(),
            "last_error": None,
            "last_attempted_at": None,
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO autostart_processes "
                    "(id, profile, command, working_dir, is_pty, created_at, last_error, last_attempted_at) "
                    "VALUES (:id, :profile, :command, :working_dir, :is_pty, :created_at, :last_error, :last_attempted_at)"
                ),
                row,
            )
        return row

    def delete(self, id: str, profile: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("DELETE FROM autostart_processes WHERE id = :id AND profile = :profile"),
                {"id": id, "profile": profile},
            )
            return cur.rowcount > 0

    def set_error(self, id: str, error: Optional[str], *, profile: Optional[str] = None) -> None:
        """Record (or clear) a registration's last start error.

        A recorded error is journalled for Cremind Tag (``automation.failed``)
        on this same connection. ``profile`` is the row's profile when the
        caller has it: a profile without Tags then costs only the cached check.
        """
        from app.tags import journal

        engine = self._engine
        if not error:
            maybe = False
        elif profile:
            maybe = journal.is_enabled_sync(engine, profile)
        else:
            maybe = bool(journal.enabled_profiles_sync(engine))
        journalled = False
        try:
            with engine.begin() as conn:
                params = {"error": error, "now": time.time(), "id": id}
                sql = (
                    "UPDATE autostart_processes "
                    "SET last_error = :error, last_attempted_at = :now WHERE id = :id"
                )
                if not maybe:
                    conn.execute(text(sql), params)
                else:
                    row = conn.execute(text(sql + " RETURNING profile, working_dir"), params).first()
                    if row is not None and journal.is_enabled_sync(engine, row.profile):
                        from app.tags.sanitize import autostart_failure, automation_failed_entry

                        # A fixed summary and the listener's name: never the
                        # process's output or its command line (either can
                        # carry secrets), which is what ``error`` holds.
                        journal.append_sync(conn, row.profile, [automation_failed_entry(
                            automation_kind="autostart", name=_listener_name(row.working_dir),
                            error=autostart_failure(error), source_id=id,
                        )])
                        journalled = True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"AutostartStorage.set_error({id}): {exc}")
        if journalled:
            journal.wake()

    def clear_error(self, id: str) -> None:
        self.set_error(id, None)
