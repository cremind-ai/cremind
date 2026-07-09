"""Synchronous storage for global server config and per-profile config.

Manages three tables:

- ``server_config``  Global server settings (key/value, optional secret flag).
- ``llm_config``     Per-profile LLM credentials and model assignments
                     (profile/key/value, optional secret flag).
- ``user_config``    Per-profile general application configuration
                     (profile/key/value, no secret flag) — drives the
                     Settings → Config UI.

After the tool-management refactor this module no longer owns the
``tool_configs`` table. Tool-related data (registry rows, profile↔tool join,
scoped per-tool configs) lives in :mod:`app.storage.tool_storage`.

All schema creation is owned by
:class:`app.storage.conversation_storage.ConversationStorage`. This class
never issues ``CREATE TABLE`` statements -- it only reads/writes existing
tables. Backend (SQLite or PostgreSQL) is selected by the active
:class:`app.databases.DatabaseProvider`; SQL goes through SQLAlchemy Core so
the same code runs on both.
"""

from __future__ import annotations

import time

from sqlalchemy import text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase, dialect_upsert


_PROFILE_SCOPED = ("llm_config", "user_config")
_SECRET_AWARE = ("server_config", "llm_config")
_VALID_TABLES = ("server_config", "llm_config", "user_config")


class DynamicConfigStorage(SyncStorageBase):
    """Sync dynamic-config storage backed by the active DatabaseProvider.

    Operates on three tables: ``server_config`` (global, secret-aware),
    ``llm_config`` (profile-scoped, secret-aware), and ``user_config``
    (profile-scoped, no secret flag).
    """

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    # ── Generic key-value operations ──

    def get(self, table: str, key: str, profile: str = "admin") -> str | None:
        if table not in _VALID_TABLES:
            return None
        with self._engine.connect() as conn:
            if table == "server_config":
                row = conn.execute(
                    text("SELECT value FROM server_config WHERE key = :key"),
                    {"key": key},
                ).fetchone()
            else:
                row = conn.execute(
                    text(f"SELECT value FROM {table} WHERE profile = :profile AND key = :key"),
                    {"profile": profile, "key": key},
                ).fetchone()
            return row[0] if row else None

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin"):
        if table not in _VALID_TABLES:
            raise ValueError(f"Invalid table: {table}")
        now = time.time() * 1000
        with self._engine.begin() as conn:
            if table == "server_config":
                stmt = dialect_upsert(
                    self._engine, "server_config",
                    values={"key": key, "value": value, "is_secret": bool(is_secret), "updated_at": now},
                    conflict_keys=["key"],
                    update_fields=["value", "is_secret", "updated_at"],
                )
            elif table == "llm_config":
                stmt = dialect_upsert(
                    self._engine, "llm_config",
                    values={"profile": profile, "key": key, "value": value, "is_secret": bool(is_secret), "updated_at": now},
                    conflict_keys=["profile", "key"],
                    update_fields=["value", "is_secret", "updated_at"],
                )
            else:  # user_config — no is_secret column
                stmt = dialect_upsert(
                    self._engine, "user_config",
                    values={"profile": profile, "key": key, "value": value, "updated_at": now},
                    conflict_keys=["profile", "key"],
                    update_fields=["value", "updated_at"],
                )
            conn.execute(stmt)

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        if table not in _VALID_TABLES:
            return False
        with self._engine.begin() as conn:
            if table == "server_config":
                result = conn.execute(
                    text("DELETE FROM server_config WHERE key = :key"),
                    {"key": key},
                )
            else:
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE profile = :profile AND key = :key"),
                    {"profile": profile, "key": key},
                )
            return result.rowcount > 0

    def delete_by_prefix(self, table: str, prefix: str, profile: str = "admin") -> int:
        """Delete all keys matching a prefix. Returns the number of rows deleted."""
        if table not in _VALID_TABLES:
            return 0
        with self._engine.begin() as conn:
            if table == "server_config":
                result = conn.execute(
                    text("DELETE FROM server_config WHERE key LIKE :pattern"),
                    {"pattern": prefix + "%"},
                )
            else:
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE profile = :profile AND key LIKE :pattern"),
                    {"profile": profile, "pattern": prefix + "%"},
                )
            return result.rowcount

    def get_all(self, table: str, include_secrets: bool = False, profile: str = "admin") -> dict[str, str]:
        if table not in _VALID_TABLES:
            return {}
        with self._engine.connect() as conn:
            if table == "server_config":
                rows = conn.execute(text("SELECT key, value, is_secret FROM server_config")).fetchall()
                secret_aware = True
            elif table == "llm_config":
                rows = conn.execute(
                    text("SELECT key, value, is_secret FROM llm_config WHERE profile = :profile"),
                    {"profile": profile},
                ).fetchall()
                secret_aware = True
            else:  # user_config
                rows = conn.execute(
                    text("SELECT key, value FROM user_config WHERE profile = :profile"),
                    {"profile": profile},
                ).fetchall()
                secret_aware = False
            result = {}
            for row in rows:
                if secret_aware and row[2] and not include_secrets:
                    result[row[0]] = "***"
                else:
                    result[row[0]] = row[1]
            return result

    def list_keys(self, table: str, profile: str = "admin") -> list[dict]:
        """Return ``[{"key", "is_secret"}]`` without reading secret values.

        Existence-only listing for callers (e.g. the blueprint exporter) that
        need to know *which* keys exist and whether each is a secret, but must
        never read secret values. ``user_config`` has no secret column, so
        ``is_secret`` is always ``False`` there.
        """
        if table not in _VALID_TABLES:
            return []
        with self._engine.connect() as conn:
            if table == "server_config":
                rows = conn.execute(text("SELECT key, is_secret FROM server_config")).fetchall()
                return [{"key": r[0], "is_secret": bool(r[1])} for r in rows]
            if table == "llm_config":
                rows = conn.execute(
                    text("SELECT key, is_secret FROM llm_config WHERE profile = :profile"),
                    {"profile": profile},
                ).fetchall()
                return [{"key": r[0], "is_secret": bool(r[1])} for r in rows]
            # user_config — no is_secret column
            rows = conn.execute(
                text("SELECT key FROM user_config WHERE profile = :profile"),
                {"profile": profile},
            ).fetchall()
            return [{"key": r[0], "is_secret": False} for r in rows]

    # ── Setup status ──

    def is_setup_complete(self) -> bool:
        return self.get("server_config", "setup_complete") == "true"

    def mark_setup_complete(self):
        self.set("server_config", "setup_complete", "true")
