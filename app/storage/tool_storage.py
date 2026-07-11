"""Synchronous storage for the tool registry.

Replaces the prior split between ``remote_agent_storage``, ``mcp_server_storage``,
and the tool_configs portion of ``DynamicConfigStorage``. Manages three tables:

- ``tools``         : the global tool registry (one row per tool_id)
- ``profile_tools`` : M:N profile↔tool visibility/enabled state (a2a/mcp only)
- ``tool_configs``  : scoped per-profile per-tool key/value config

All schema creation is owned by :class:`app.storage.conversation_storage.ConversationStorage`
which is initialized first; this class never issues ``CREATE TABLE`` statements.
The backend (SQLite or PostgreSQL) is provided by the active
:class:`app.databases.DatabaseProvider`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import text

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase, dialect_upsert
from app.utils.logger import logger

SCOPE_ARG = "arg"
SCOPE_VARIABLE = "variable"
SCOPE_META = "meta"
# ``leaf`` stores per-profile enable/disable for a built-in/MCP group's
# individual sub-tools ("leaves"). Opt-out model: a row exists only for a
# DISABLED leaf (key=leaf_name, value="false"); absence == enabled.
SCOPE_LEAF = "leaf"
VALID_SCOPES = {SCOPE_ARG, SCOPE_VARIABLE, SCOPE_META, SCOPE_LEAF}


class ToolStorage(SyncStorageBase):
    """Sync storage for tools, profile_tools, and tool_configs."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    # ── tools ──────────────────────────────────────────────────────────────

    def upsert_tool(
        self,
        *,
        tool_id: str,
        name: str,
        tool_type: str,
        source: str | None = None,
        description: str | None = None,
        arguments_schema: dict | None = None,
        extra: dict | None = None,
        owner_profile: str | None = None,
    ) -> None:
        """Insert or update a tool row keyed by ``tool_id``."""
        now = time.time() * 1000
        values = {
            "tool_id": tool_id,
            "name": name,
            "tool_type": tool_type,
            "source": source,
            "description": description,
            "arguments_schema": json.dumps(arguments_schema) if arguments_schema is not None else None,
            "extra": json.dumps(extra) if extra is not None else None,
            "owner_profile": owner_profile,
            "created_at": now,
            "updated_at": now,
        }
        with self._engine.begin() as conn:
            stmt = dialect_upsert(
                self._engine, "tools",
                values=values,
                conflict_keys=["tool_id"],
                update_fields=[
                    "name", "tool_type", "source", "description",
                    "arguments_schema", "extra", "owner_profile", "updated_at",
                ],
            )
            conn.execute(stmt)

    def get_tool(self, tool_id: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM tools WHERE tool_id = :tool_id"),
                {"tool_id": tool_id},
            ).mappings().fetchone()
            return self._row_to_tool(row) if row else None

    def find_tool_by_source(self, tool_type: str, source: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM tools WHERE tool_type = :tool_type AND source = :source"),
                {"tool_type": tool_type, "source": source},
            ).mappings().fetchone()
            return self._row_to_tool(row) if row else None

    def list_tools(self, tool_type: str | None = None) -> list[dict]:
        with self._engine.connect() as conn:
            if tool_type:
                rows = conn.execute(
                    text("SELECT * FROM tools WHERE tool_type = :tool_type ORDER BY name"),
                    {"tool_type": tool_type},
                ).mappings().fetchall()
            else:
                rows = conn.execute(
                    text("SELECT * FROM tools ORDER BY tool_type, name")
                ).mappings().fetchall()
            return [self._row_to_tool(r) for r in rows]

    def list_tool_ids_by_type(self, tool_type: str) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT tool_id FROM tools WHERE tool_type = :tool_type"),
                {"tool_type": tool_type},
            ).fetchall()
            return [r[0] for r in rows]

    def list_existing_tool_ids(self) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT tool_id FROM tools")).fetchall()
            return {r[0] for r in rows}

    def delete_tool(self, tool_id: str) -> bool:
        """Remove a tool. Cascades to profile_tools, tool_configs."""
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("DELETE FROM tools WHERE tool_id = :tool_id"),
                {"tool_id": tool_id},
            )
            return cur.rowcount > 0

    def rename_tool(self, old_tool_id: str, new_tool_id: str) -> bool:
        """Atomically rename a tool's primary key.

        Used to displace a previously-persisted dynamic tool (skill / a2a /
        mcp) when a fixed-name tool (intrinsic / built-in) claims its slug.
        Relies on ``ON UPDATE CASCADE`` to migrate ``profile_tools`` and
        ``tool_configs`` rows along with the rename.

        Returns True on success, False if the new id is already taken or the
        old id wasn't found.
        """
        if old_tool_id == new_tool_id:
            return True
        now = time.time() * 1000
        with self._engine.begin() as conn:
            taken = conn.execute(
                text("SELECT 1 FROM tools WHERE tool_id = :tool_id"),
                {"tool_id": new_tool_id},
            ).fetchone()
            if taken:
                return False
            cur = conn.execute(
                text("UPDATE tools SET tool_id = :new, updated_at = :now WHERE tool_id = :old"),
                {"new": new_tool_id, "now": now, "old": old_tool_id},
            )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_tool(row: Any) -> dict:
        return {
            "tool_id": row["tool_id"],
            "name": row["name"],
            "tool_type": row["tool_type"],
            "source": row["source"],
            "description": row["description"],
            "arguments_schema": json.loads(row["arguments_schema"]) if row["arguments_schema"] else None,
            "extra": json.loads(row["extra"]) if row["extra"] else None,
            "owner_profile": row["owner_profile"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ── profile_tools ──────────────────────────────────────────────────────

    def set_profile_tool(self, profile: str, tool_id: str, enabled: bool) -> None:
        """Upsert a (profile, tool_id) row with the given enabled flag."""
        now = time.time() * 1000
        with self._engine.begin() as conn:
            stmt = dialect_upsert(
                self._engine, "profile_tools",
                values={"profile": profile, "tool_id": tool_id, "enabled": bool(enabled), "added_at": now},
                conflict_keys=["profile", "tool_id"],
                update_fields=["enabled"],
            )
            conn.execute(stmt)

    def get_profile_tool_enabled(self, profile: str, tool_id: str) -> bool | None:
        """Return enabled flag, or None if no row exists."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT enabled FROM profile_tools WHERE profile = :profile AND tool_id = :tool_id"),
                {"profile": profile, "tool_id": tool_id},
            ).fetchone()
            return bool(row[0]) if row else None

    def list_profile_tools(self, profile: str) -> dict[str, bool]:
        """Return ``{tool_id: enabled}`` for all rows belonging to ``profile``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT tool_id, enabled FROM profile_tools WHERE profile = :profile"),
                {"profile": profile},
            ).fetchall()
            return {r[0]: bool(r[1]) for r in rows}

    def list_tool_ids_enabled_by_any_profile(self) -> set[str]:
        """Return tool_ids that at least one profile has marked enabled.

        Used at startup to decide whether an A2A/MCP tool should be connected
        (present in the set) or registered as a stub (absent). Tools with no
        ``profile_tools`` row at all are correctly excluded -- A2A/MCP tools
        default to disabled when no row exists.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT tool_id FROM profile_tools WHERE enabled = :enabled"),
                {"enabled": True},
            ).fetchall()
            return {r[0] for r in rows}

    def delete_profile_tool(self, profile: str, tool_id: str) -> bool:
        with self._engine.begin() as conn:
            cur = conn.execute(
                text("DELETE FROM profile_tools WHERE profile = :profile AND tool_id = :tool_id"),
                {"profile": profile, "tool_id": tool_id},
            )
            return cur.rowcount > 0

    def backfill_profile_tools_for_new_profile(self, profile: str) -> int:
        """Insert disabled rows in ``profile_tools`` for every existing a2a/mcp tool.

        Skipped tool_ids that already have a row are left alone. Built-in and
        skill tools are not added (they are implicitly available everywhere).
        Returns the number of rows inserted.
        """
        now = time.time() * 1000
        with self._engine.begin() as conn:
            tool_rows = conn.execute(
                text("SELECT tool_id FROM tools WHERE tool_type IN ('a2a', 'mcp')")
            ).fetchall()
            existing = {
                r[0] for r in conn.execute(
                    text("SELECT tool_id FROM profile_tools WHERE profile = :profile"),
                    {"profile": profile},
                ).fetchall()
            }
            inserted = 0
            for r in tool_rows:
                if r[0] in existing:
                    continue
                conn.execute(
                    text(
                        "INSERT INTO profile_tools (profile, tool_id, enabled, added_at) "
                        "VALUES (:profile, :tool_id, :enabled, :added_at)"
                    ),
                    {"profile": profile, "tool_id": r[0], "enabled": False, "added_at": now},
                )
                inserted += 1
            return inserted

    def backfill_profile_tools_for_new_tool(
        self, tool_id: str, owner_profile: str | None,
    ) -> None:
        """When a new a2a/mcp tool is registered, add a row for every profile.

        The owner profile gets ``enabled=1``; everyone else gets ``enabled=0``.
        """
        now = time.time() * 1000
        with self._engine.begin() as conn:
            profile_rows = conn.execute(text("SELECT name FROM profiles")).fetchall()
            existing_profiles = {
                r[0] for r in conn.execute(
                    text("SELECT profile FROM profile_tools WHERE tool_id = :tool_id"),
                    {"tool_id": tool_id},
                ).fetchall()
            }
            for r in profile_rows:
                profile = r[0]
                if profile.startswith("__"):
                    continue
                if profile in existing_profiles:
                    continue
                enabled = profile == owner_profile
                conn.execute(
                    text(
                        "INSERT INTO profile_tools (profile, tool_id, enabled, added_at) "
                        "VALUES (:profile, :tool_id, :enabled, :added_at)"
                    ),
                    {"profile": profile, "tool_id": tool_id, "enabled": enabled, "added_at": now},
                )

    # ── tool_configs ───────────────────────────────────────────────────────

    @staticmethod
    def _check_scope(scope: str) -> None:
        if scope not in VALID_SCOPES:
            raise ValueError(f"Invalid tool_config scope '{scope}'. Must be one of {VALID_SCOPES}.")

    def set_config(
        self,
        *,
        profile: str,
        tool_id: str,
        scope: str,
        key: str,
        value: str,
        is_secret: bool = False,
    ) -> None:
        self._check_scope(scope)
        now = time.time() * 1000
        with self._engine.begin() as conn:
            stmt = dialect_upsert(
                self._engine, "tool_configs",
                values={
                    "profile": profile, "tool_id": tool_id, "scope": scope, "key": key,
                    "value": value, "is_secret": bool(is_secret), "updated_at": now,
                },
                conflict_keys=["profile", "tool_id", "scope", "key"],
                update_fields=["value", "is_secret", "updated_at"],
            )
            conn.execute(stmt)

    def get_config(
        self, *, profile: str, tool_id: str, scope: str, key: str,
    ) -> str | None:
        self._check_scope(scope)
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT value FROM tool_configs "
                    "WHERE profile = :profile AND tool_id = :tool_id AND scope = :scope AND key = :key"
                ),
                {"profile": profile, "tool_id": tool_id, "scope": scope, "key": key},
            ).fetchone()
            return row[0] if row else None

    def get_scope(
        self,
        *,
        profile: str,
        tool_id: str,
        scope: str,
        include_secrets: bool = False,
    ) -> dict[str, str]:
        """Return all ``{key: value}`` entries in ``scope`` for one tool."""
        self._check_scope(scope)
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT key, value, is_secret FROM tool_configs "
                    "WHERE profile = :profile AND tool_id = :tool_id AND scope = :scope"
                ),
                {"profile": profile, "tool_id": tool_id, "scope": scope},
            ).fetchall()
            result: dict[str, str] = {}
            for r in rows:
                if r[2] and not include_secrets:
                    result[r[0]] = "***"
                else:
                    result[r[0]] = r[1]
            return result

    def get_all_scopes(
        self,
        *,
        profile: str,
        tool_id: str,
        include_secrets: bool = False,
    ) -> dict[str, dict[str, str]]:
        """Return ``{scope: {key: value}}`` for one tool."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT scope, key, value, is_secret FROM tool_configs "
                    "WHERE profile = :profile AND tool_id = :tool_id"
                ),
                {"profile": profile, "tool_id": tool_id},
            ).fetchall()
            result: dict[str, dict[str, str]] = {}
            for r in rows:
                bucket = result.setdefault(r[0], {})
                if r[3] and not include_secrets:
                    bucket[r[1]] = "***"
                else:
                    bucket[r[1]] = r[2]
            return result

    def delete_config(
        self, *, profile: str, tool_id: str, scope: str, key: str,
    ) -> bool:
        self._check_scope(scope)
        with self._engine.begin() as conn:
            cur = conn.execute(
                text(
                    "DELETE FROM tool_configs "
                    "WHERE profile = :profile AND tool_id = :tool_id AND scope = :scope AND key = :key"
                ),
                {"profile": profile, "tool_id": tool_id, "scope": scope, "key": key},
            )
            return cur.rowcount > 0

    def list_disabled_leaves(self, profile: str) -> dict[str, set[str]]:
        """Return ``{tool_id: {leaf_name, ...}}`` of disabled leaves for ``profile``.

        Single grouped query over the ``leaf`` config scope -- mirrors
        :meth:`list_profile_tools` so the reasoning agent can resolve every
        tool's disabled sub-tools in one read per turn rather than one per tool.
        Only DISABLED leaves are persisted (value ``"false"``); enabled leaves
        have no row and fall through to the default (on).
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tool_id, key FROM tool_configs "
                    "WHERE profile = :profile AND scope = :scope AND value = :value"
                ),
                {"profile": profile, "scope": SCOPE_LEAF, "value": "false"},
            ).fetchall()
            out: dict[str, set[str]] = {}
            for tool_id, key in rows:
                out.setdefault(tool_id, set()).add(key)
            return out

    def list_config_keys(self, profile: str) -> list[dict]:
        """Return ``[{tool_id, scope, key, is_secret}]`` for a profile's configs.

        Existence-only listing (values are never read) — used by the blueprint
        exporter to discover which tools a profile has configured and which
        config keys are secrets, without ever pulling a secret value.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tool_id, scope, key, is_secret FROM tool_configs "
                    "WHERE profile = :profile"
                ),
                {"profile": profile},
            ).fetchall()
            return [
                {"tool_id": r[0], "scope": r[1], "key": r[2], "is_secret": bool(r[3])}
                for r in rows
            ]

    def delete_all_configs(self, *, profile: str, tool_id: str) -> int:
        """Delete every config row (all scopes) for one (profile, tool_id).

        Used to wipe a skill's saved Variables/Arguments/LLM/meta on
        "Reset to Default" -- the on-disk files are restored from the shipped
        built-in, and this returns the per-profile configuration to its
        pristine, unconfigured state. Returns the number of rows removed.
        """
        with self._engine.begin() as conn:
            cur = conn.execute(
                text(
                    "DELETE FROM tool_configs "
                    "WHERE profile = :profile AND tool_id = :tool_id"
                ),
                {"profile": profile, "tool_id": tool_id},
            )
            return cur.rowcount


# ── singleton ──────────────────────────────────────────────────────────────

_instance: ToolStorage | None = None


def get_tool_storage(provider: DatabaseProvider | None = None) -> ToolStorage:
    global _instance
    if _instance is None:
        _instance = ToolStorage(provider)
        logger.debug("Initialized ToolStorage")
    return _instance


def _reset_tool_storage_singleton() -> None:
    """Used by the wizard hot-swap to drop the cached engine reference."""
    global _instance
    _instance = None
