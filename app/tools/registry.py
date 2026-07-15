"""Central registry for all tools (intrinsic / built-in / a2a / mcp / skill).

The registry is the single source of truth for what tools exist and what
their tool_ids are. It owns:

- An in-memory ``tool_id -> Tool`` map (used by the reasoning agent dispatch).
- The persisted ``tools`` table for the four registered types
  (intrinsic tools are not persisted -- they're discovered from code at every
  startup).
- The per-profile ``profile_tools`` rows (a2a/mcp visibility).
- Skill synchronisation: at startup and at hot-reload, ``sync_skills`` diffs
  the current on-disk skills against the persisted rows and adds/updates/
  removes as appropriate -- guarded by an ``asyncio.Lock`` so reloads can't
  interleave with the reasoning loop.

Profile-availability rules:
- intrinsic / builtin : always available to every profile (no
  ``profile_tools`` row).
- skill               : owned by exactly one profile -- ``tool_id`` is
  prefixed ``<profile>__<slug>`` and ``tools.owner_profile`` records the
  owner. Only the owning profile sees the skill.
- a2a / mcp : visible to every profile; enabled only where
  ``profile_tools.enabled = 1``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.storage.tool_storage import ToolStorage
from app.tools.base import Tool, ToolType
from app.tools.config_manager import ToolConfigManager
from app.tools.ids import (
    ToolIdConflictError,
    allocate_fixed_tool_id,
    allocate_unique_tool_id,
    slugify,
)
from app.utils.logger import logger


class ToolRegistry:
    """Single source of truth for tools.

    Lifecycle
    ---------
    1. Built at server startup with a :class:`ToolStorage` and
       :class:`ToolConfigManager`.
    2. Intrinsic / built-in / skill tools are registered during startup.
    3. A2A / MCP / Skill registrations during runtime mutate the in-memory map
       *and* the database (atomically inside the registry's lock).
    """

    def __init__(self, tool_storage: ToolStorage, config_manager: ToolConfigManager):
        self._storage = tool_storage
        self._config = config_manager
        # Strong refs to live Tool instances.
        self._tools: Dict[str, Tool] = {}
        # Async lock guarding all multi-step mutations (sync_skills, register_*).
        self._lock = asyncio.Lock()
        # Optional callback fired after add/remove/sync mutations -- used by
        # the CremindAgent to refresh its embedding tables. The callback may
        # accept an optional ``profile`` kwarg; when set, only that profile's
        # embedding table needs rebuilding (skill sync). When ``None``, the
        # mutation affects every profile (builtin rebind, a2a/mcp change).
        self._on_change: Optional[Callable[..., None]] = None

    # ── observability ──────────────────────────────────────────────────

    def set_change_callback(self, cb: Optional[Callable[..., None]]) -> None:
        self._on_change = cb

    def _fire_change(self, profile: Optional[str] = None) -> None:
        if self._on_change:
            try:
                self._on_change(profile=profile)
            except TypeError:
                # Back-compat: callback that takes no arguments.
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001
                    logger.exception("ToolRegistry change callback failed")
            except Exception:  # noqa: BLE001
                logger.exception("ToolRegistry change callback failed")

    @property
    def storage(self) -> ToolStorage:
        return self._storage

    @property
    def config(self) -> ToolConfigManager:
        return self._config

    # ── lookup ─────────────────────────────────────────────────────────

    def get(self, tool_id: str) -> Optional[Tool]:
        return self._tools.get(tool_id)

    def all_tools(self) -> List[Tool]:
        return list(self._tools.values())

    @staticmethod
    def _default_enabled(tool: Tool) -> bool:
        """Default enabled state when no ``profile_tools`` row exists.

        - builtin : per-tool ``default`` flag (default on) — declared via
                    ``TOOL_CONFIG["default"]`` (see ToolConfig.default), so a
                    built-in can opt to start disabled.
        - skill   : on by default *for its owner profile*; invisible to other
                    profiles (filtered in :meth:`tools_for_profile`). The Setup
                    Wizard shows skills off (see :meth:`visible_for_profile`),
                    but that is a wizard-display default only — the runtime
                    fallback here stays on so skills imported/created outside the
                    wizard keep working.
        - mcp     : off by default (opt-in per profile, except for the owner
                    profile which gets enabled=1 at registration)
        """
        if tool.tool_type is ToolType.BUILTIN:
            return tool.default_enabled
        return tool.tool_type is ToolType.SKILL

    @staticmethod
    def _skill_belongs_to_profile(tool: Tool, profile: str) -> bool:
        """Return True if ``tool`` is a skill owned by ``profile``.

        Ownership is encoded in the tool_id prefix ``<profile>__``.
        """
        return (
            tool.tool_type is ToolType.SKILL
            and tool.tool_id.startswith(f"{profile}__")
        )

    def is_enabled_for_profile(self, tool: Tool, profile: str) -> bool:
        """Resolve a tool's enabled state for ``profile``.

        Looks at ``profile_tools`` for an explicit value; falls back to the
        per-type default.
        """
        explicit = self._storage.get_profile_tool_enabled(profile, tool.tool_id)
        if explicit is not None:
            return explicit
        return self._default_enabled(tool)

    def tools_for_profile(self, profile: str) -> List[Tool]:
        """Return tools the reasoning agent should expose to ``profile``.

        Skills owned by other profiles are excluded. Every type — skills
        included — honors a per-profile ``profile_tools`` row when present,
        falling back to the type's default (built-in = on; skill = on;
        mcp = off), so a skill disabled in Settings is no longer exposed.
        """
        enabled_per_profile = self._storage.list_profile_tools(profile)
        out: List[Tool] = []
        for tool in self._tools.values():
            # Hidden system built-ins are always-on for every profile —
            # their lifecycle is the runtime's, not the user's, so a stale
            # ``profile_tools`` row must not be able to suppress them.
            if tool.hidden:
                out.append(tool)
                continue
            # ``locked`` built-ins are visible in the UI but can't be disabled;
            # always expose them so a stale disabled row can't suppress them.
            if tool.locked:
                out.append(tool)
                continue
            if tool.tool_type is ToolType.SKILL:
                if not self._skill_belongs_to_profile(tool, profile):
                    continue
                # owned skill — fall through to the shared enabled check so a
                # ``profile_tools`` row with enabled=0 (set via Settings)
                # suppresses it just like any other tool. Absent a row, the
                # SKILL default (on) keeps it exposed.
            explicit = enabled_per_profile.get(tool.tool_id)
            enabled = explicit if explicit is not None else self._default_enabled(tool)
            if enabled:
                out.append(tool)
        return out

    def owned_skills(self, profile: str) -> List[Tool]:
        """Return every skill owned by ``profile`` regardless of enabled state.

        Unlike :meth:`tools_for_profile`, this ignores the per-profile
        ``enabled`` flag — callers that operate on a profile's skills as
        on-disk assets (e.g. re-materializing ``scripts/.env``) need the full
        set, not just the ones currently exposed to the agent.
        """
        return [
            tool
            for tool in self._tools.values()
            if self._skill_belongs_to_profile(tool, profile)
        ]

    def visible_for_profile(self, profile: str) -> List[dict]:
        """Return UI rows for ``profile`` -- includes disabled stubs.

        Used by ``GET /api/tools``. Hidden tools (intrinsic) are excluded.
        Skills owned by other profiles are excluded entirely -- each profile
        only sees its own skills.
        """
        enabled_per_profile = self._storage.list_profile_tools(profile)
        rows: List[dict] = []
        for tool in self._tools.values():
            if tool.hidden:
                continue
            if tool.tool_type is ToolType.SKILL and not self._skill_belongs_to_profile(tool, profile):
                continue
            explicit = enabled_per_profile.get(tool.tool_id)
            enabled = explicit if explicit is not None else self._default_enabled(tool)
            # ``locked`` tools can't be disabled, so always report them on —
            # a stale ``profile_tools`` row must not show them as off.
            if tool.locked:
                enabled = True
            rows.append({
                "tool_id": tool.tool_id,
                "name": tool.name,
                "description": tool.description,
                "tool_type": tool.tool_type.value,
                "enabled": enabled,
                # Profile-independent default (ignores any ``profile_tools``
                # override) so callers can show pristine defaults instead of
                # the caller's resolved state — used by the Setup Wizard when
                # configuring a brand-new profile. Skills start OFF in the wizard
                # (the admin opts each one in — this is a wizard-display default
                # only; the runtime skill fallback stays on, see
                # :meth:`_default_enabled`). Locked tools always report on;
                # otherwise built-ins honor their per-tool ``default`` flag.
                "default_enabled": (
                    False if tool.tool_type is ToolType.SKILL
                    else (True if tool.locked else self._default_enabled(tool))
                ),
                "arguments_schema": tool.arguments_schema,
            })
        return rows

    # ── id allocation ──────────────────────────────────────────────────

    def _taken(self) -> set[str]:
        return set(self._tools.keys())

    def repersist_builtin_tools(self) -> int:
        """Re-write rows for every in-memory built-in tool to the storage backend.

        Used by the setup wizard after the admin picks PostgreSQL: built-in
        tools were registered into the boot-time SQLite DB, and the freshly
        created Postgres ``tools`` table is empty. Without re-persisting,
        any later ``tool_configs`` write that FK-references a built-in slug
        fails with a ForeignKeyViolation.

        Returns the number of rows upserted. Idempotent — safe to call when
        not actually needed.
        """
        count = 0
        for tool_id, tool in self._tools.items():
            if tool.tool_type is not ToolType.BUILTIN:
                continue
            self._storage.upsert_tool(
                tool_id=tool_id,
                name=tool.name,
                tool_type=ToolType.BUILTIN.value,
                source=tool_id,
                description=tool.description,
                arguments_schema=tool.arguments_schema,
            )
            count += 1
        return count

    # ── built-in registration (persisted, no profile_tools row) ────────

    def register_builtin(self, tool: Tool, *, source: str | None = None) -> str:
        if tool.tool_type is not ToolType.BUILTIN:
            raise ValueError("register_builtin requires a Tool with tool_type=BUILTIN")
        tool_id = allocate_fixed_tool_id(tool.name, self._taken())
        # Built-in tools have FIXED slugs and take precedence over any
        # previously-persisted dynamic tool (skill / a2a / mcp) that happens
        # to claim the same slug. Atomically rename the displaced row so its
        # per-profile state (profile_tools + tool_configs) survives via
        # ON UPDATE CASCADE.
        self._displace_persisted_collision(tool_id, claimant_name=tool.name)
        tool.tool_id = tool_id
        self._tools[tool_id] = tool
        self._storage.upsert_tool(
            tool_id=tool_id,
            name=tool.name,
            tool_type=ToolType.BUILTIN.value,
            source=source or tool_id,
            description=tool.description,
            arguments_schema=tool.arguments_schema,
        )
        logger.info(f"Registered built-in tool '{tool.name}' (tool_id={tool_id})")
        return tool_id

    def _displace_persisted_collision(self, tool_id: str, *, claimant_name: str) -> None:
        """If ``tool_id`` is held by a non-fixed (dynamic) tool in the DB,
        rename that row so the fixed tool can claim the slug.

        - intrinsic / builtin (fixed)  : never renamed (would have been caught
          earlier by ``allocate_fixed_tool_id``, which raises on collision).
        - skill / a2a / mcp (dynamic)  : renamed with a ``_2`` / ``_3`` suffix.
          Per-profile rows follow via ``ON UPDATE CASCADE``.
        """
        existing = self._storage.get_tool(tool_id)
        if existing is None:
            return
        existing_type = existing["tool_type"]
        if existing_type == ToolType.BUILTIN.value:
            # Same fixed type re-registered (idempotent startup) -- nothing to do.
            return
        # Dynamic tool currently holds the slug; rename it.
        displaced_name = existing["name"]
        new_id = allocate_unique_tool_id(displaced_name, self._taken() | {tool_id})
        if not self._storage.rename_tool(tool_id, new_id):
            # Should not happen -- if rename fails, fall back to delete so the
            # built-in can still register. A skill row has no per-profile state
            # so this is safe; for a2a/mcp we'd lose state, which is logged.
            logger.warning(
                f"Could not rename displaced {existing_type} tool '{displaced_name}' "
                f"({tool_id} -> {new_id}). Deleting row instead; per-profile "
                f"state for that tool will be lost."
            )
            self._storage.delete_tool(tool_id)
            return
        # Mirror the rename in any in-memory entry that may have been hydrated
        # earlier in the startup sequence.
        old_in_memory = self._tools.pop(tool_id, None)
        if old_in_memory is not None:
            old_in_memory.tool_id = new_id
            self._tools[new_id] = old_in_memory
        logger.warning(
            f"Fixed-name tool '{claimant_name}' (tool_id='{tool_id}') displaced "
            f"persisted {existing_type} tool '{displaced_name}'. "
            f"Renamed displaced tool to '{new_id}' (per-profile state preserved)."
        )

    # ── mcp registration (persisted, profile_tools backfilled) ──

    async def register_mcp(
        self, tool: Tool, *, source: str, owner_profile: str | None, extra: dict | None = None,
    ) -> str:
        if tool.tool_type is not ToolType.MCP:
            raise ValueError("register_mcp requires a Tool with tool_type=MCP")
        async with self._lock:
            tool_id = self._register_dynamic(
                tool, source=source, owner_profile=owner_profile,
                tool_type_value=ToolType.MCP.value, extra=extra,
            )
        self._fire_change()
        return tool_id

    def _register_dynamic(
        self,
        tool: Tool,
        *,
        source: str,
        owner_profile: str | None,
        tool_type_value: str,
        extra: dict | None = None,
    ) -> str:
        # Reuse existing tool_id if this (type, source) was registered before
        existing = self._storage.find_tool_by_source(tool_type_value, source)
        if existing:
            tool_id = existing["tool_id"]
        else:
            tool_id = allocate_unique_tool_id(tool.name, self._taken())
        tool.tool_id = tool_id
        self._tools[tool_id] = tool
        self._storage.upsert_tool(
            tool_id=tool_id,
            name=tool.name,
            tool_type=tool_type_value,
            source=source,
            description=tool.description,
            arguments_schema=tool.arguments_schema,
            extra=extra,
            owner_profile=owner_profile,
        )
        # Ensure every profile has a row: owner enabled, others disabled
        self._storage.backfill_profile_tools_for_new_tool(tool_id, owner_profile)
        logger.info(
            f"Registered {tool_type_value} tool '{tool.name}' "
            f"(tool_id={tool_id}, owner={owner_profile})"
        )
        return tool_id

    # ── skills (persisted, no profile_tools row; owned by a profile) ───

    def _unique_skill_id(self, preferred: str, *, ignore: Optional[str] = None) -> str:
        """Return ``preferred`` if free, else suffix ``_2``, ``_3`` ... .

        ``ignore`` lets the caller mark one existing id as "don't count as
        taken" -- used during rename when we want to keep the current slot
        reachable as a candidate for the new name.
        """
        taken = self._taken()
        if ignore is not None:
            taken.discard(ignore)
        if preferred not in taken:
            return preferred
        n = 2
        while f"{preferred}_{n}" in taken:
            n += 1
        return f"{preferred}_{n}"

    def register_skill_sync(
        self,
        tool: Tool,
        *,
        source: str,
        owner_profile: str,
    ) -> str:
        """Synchronous skill registration helper used from inside ``sync_skills``.

        Tool-ids are profile-scoped: ``<owner_profile>__<slug(tool.name)>``.
        Rules, in order:

        1. If this skill's ``source`` is already persisted, reuse its existing
           ``tool_id`` unless a fixed-name tool (intrinsic / built-in) now
           owns that slug -- in which case rename to a fresh profile-prefixed
           id.
        2. If the source is new, allocate ``<profile>__<slug>`` (suffixed on
           collision).
        """
        if tool.tool_type is not ToolType.SKILL:
            raise ValueError("register_skill_sync requires a Tool with tool_type=SKILL")

        preferred = f"{owner_profile}__{slugify(tool.name)}"
        existing = self._storage.find_tool_by_source(ToolType.SKILL.value, source)
        chosen_id: str
        if existing:
            candidate = existing["tool_id"]
            owner = self._tools.get(candidate)
            owner_is_other_fixed = (
                owner is not None
                and owner is not tool
                and owner.tool_type is ToolType.BUILTIN
            )
            if owner_is_other_fixed:
                fresh = self._unique_skill_id(preferred)
                if not self._storage.rename_tool(candidate, fresh):
                    self._storage.delete_tool(candidate)
                logger.warning(
                    f"Skill '{tool.name}' (owner='{owner_profile}') previously held "
                    f"tool_id='{candidate}', which is now owned by a fixed-name tool. "
                    f"Renamed skill to '{fresh}'."
                )
                chosen_id = fresh
            else:
                chosen_id = candidate
        else:
            chosen_id = self._unique_skill_id(preferred)

        tool.tool_id = chosen_id
        self._tools[chosen_id] = tool
        self._storage.upsert_tool(
            tool_id=chosen_id,
            name=tool.name,
            tool_type=ToolType.SKILL.value,
            source=source,
            description=tool.description,
            arguments_schema=tool.arguments_schema,
            owner_profile=owner_profile,
        )
        logger.info(
            f"Registered skill '{tool.name}' (tool_id={chosen_id}, owner={owner_profile})"
        )
        return chosen_id

    # ── in-memory replace (stub ↔ live) ────────────────────────────────

    async def replace_tool(self, tool_id: str, new_tool: Tool) -> None:
        """Swap the in-memory Tool instance for ``tool_id`` in place.

        Used when a stub is upgraded to a live connection (lazy connect) or
        when a live tool is re-built (reconnect). The persisted ``tools`` row
        and all ``profile_tools`` / ``tool_configs`` rows are left untouched --
        only the in-memory mapping changes. This avoids the cascade-delete
        that ``unregister`` + ``register_*`` would cause, which previously
        wiped every profile's enabled state except the owner's.

        Fires ``_on_change`` so the CremindAgent rebuilds its embedding table
        (the agent skips stubs, so a stub → live swap must re-emit embeddings
        to make the newly-connected tool routable).
        """
        async with self._lock:
            if tool_id not in self._tools:
                raise KeyError(f"Tool '{tool_id}' is not registered")
            new_tool.tool_id = tool_id
            self._tools[tool_id] = new_tool
        self._fire_change()

    # ── unregister ─────────────────────────────────────────────────────

    async def unregister(self, tool_id: str) -> bool:
        async with self._lock:
            existed = self._tools.pop(tool_id, None) is not None
            self._storage.delete_tool(tool_id)
        if existed:
            self._fire_change()
        return existed

    # ── skill sync ─────────────────────────────────────────────────────

    async def sync_skills(
        self,
        profile: str,
        current: Dict[str, Any],
        skill_factory: Callable[[Any], Tool],
    ) -> None:
        """Diff persisted skills for ``profile`` against the on-disk set.

        Args
        ----
        profile : owning profile whose skills are being reconciled.
        current : ``{name: SkillInfo}`` from a fresh scan of that profile's
                  skills directory.
        skill_factory : callable that turns a ``SkillInfo`` into a ``Tool``.

        For each entry owned by ``profile``:
        - existing source (dir_path) → reuse tool_id, refresh name/description.
        - new source                 → allocate fresh tool_id, register.
        - source vanished from disk  → remove tool (cascades configs).

        Skills belonging to other profiles are left untouched.
        """
        async with self._lock:
            persisted = self._storage.list_tools(tool_type=ToolType.SKILL.value)
            # Scope diff to rows owned by this profile. Rows without an
            # ``owner_profile`` (legacy data) are ignored here -- the startup
            # purge handles those.
            persisted_by_source = {
                row["source"]: row
                for row in persisted
                if row.get("owner_profile") == profile
            }

            current_sources = {str(info.dir_path) for info in current.values()}

            # Remove skills for this profile whose on-disk dir vanished.
            for source, row in persisted_by_source.items():
                if source in current_sources:
                    continue
                tool_id = row["tool_id"]
                self._tools.pop(tool_id, None)
                self._storage.delete_tool(tool_id)
                logger.info(
                    f"Removed skill (no longer on disk): tool_id={tool_id}, "
                    f"profile={profile}"
                )

            # Drop in-memory skill entries for this profile whose source is stale.
            stale_in_memory = [
                t.tool_id for t in list(self._tools.values())
                if t.tool_type is ToolType.SKILL
                and t.tool_id.startswith(f"{profile}__")
                and getattr(t, "_source", None) not in current_sources
            ]
            for tid in stale_in_memory:
                self._tools.pop(tid, None)

            # Add / update current skills.
            for info in current.values():
                source = str(info.dir_path)
                tool = skill_factory(info)
                self.register_skill_sync(tool, source=source, owner_profile=profile)

        self._fire_change(profile=profile)

    def purge_legacy_skill_rows(self, valid_sources: Iterable[str]) -> int:
        """Remove persisted skill rows whose source is not under any known
        profile's skills directory.

        Returns the number of rows deleted. Runs synchronously -- invoked once
        at startup, before any per-profile ``sync_skills`` call, so no lock is
        needed (registry is still empty of skill tools at this point).
        """
        valid = tuple(str(s) for s in valid_sources)
        removed = 0
        for row in self._storage.list_tools(tool_type=ToolType.SKILL.value):
            source = row.get("source") or ""
            if any(source.startswith(prefix) for prefix in valid):
                continue
            tool_id = row["tool_id"]
            self._tools.pop(tool_id, None)
            self._storage.delete_tool(tool_id)
            removed += 1
            logger.info(
                f"Purged legacy skill row: tool_id={tool_id}, source={source}"
            )
        return removed

    # ── profile lifecycle hooks ────────────────────────────────────────

    def on_profile_created(self, profile: str) -> int:
        """Backfill ``profile_tools`` rows for the new profile (a2a/mcp only).

        Returns the number of rows inserted.
        """
        return self._storage.backfill_profile_tools_for_new_profile(profile)

    def set_profile_tool_enabled(
        self, profile: str, tool_id: str, enabled: bool,
    ) -> None:
        """Set the per-profile enabled flag for a tool.

        Refuses intrinsic tools (they're hidden and always-on by definition)
        and any built-in marked ``hidden`` via ``TOOL_CONFIG`` — those are
        system tools whose lifecycle is controlled by the runtime, and
        disabling them would break internal flows (e.g. skill-event routing).
        Built-ins marked ``locked`` are visible but can't be turned off, so a
        disable is refused while an (idempotent) re-enable is allowed. Every
        other type accepts the toggle; the row in ``profile_tools`` overrides
        the per-type default returned by :meth:`_default_enabled`.
        """
        tool = self._tools.get(tool_id)
        if tool is None:
            raise KeyError(f"Tool '{tool_id}' is not registered")
        if tool.hidden:
            raise ValueError(
                f"Tool '{tool_id}' is a hidden system tool and cannot be "
                f"toggled from the Settings UI."
            )
        # ``locked`` tools are visible but always-on: a re-enable is a no-op,
        # but a disable must be refused.
        if tool.locked and not enabled:
            raise ValueError(
                f"Tool '{tool_id}' is locked and cannot be disabled."
            )
        self._storage.set_profile_tool(profile, tool_id, enabled)

    # ── per-leaf enable/disable (sub-tools of a built-in/MCP group) ─────

    def disabled_leaves_by_tool(self, profile: str) -> Dict[str, set[str]]:
        """Return ``{tool_id: {leaf_name, ...}}`` of disabled leaves for ``profile``.

        Used by the reasoning agent's per-step tool build to suppress disabled
        sub-tools in one read, mirroring :meth:`tools_for_profile`'s use of
        ``list_profile_tools``.
        """
        return self._storage.list_disabled_leaves(profile)

    def leaves_for_profile(self, profile: str, tool_id: str) -> dict:
        """Return the UI payload describing a tool's sub-tools for ``profile``.

        Built from the tool's ``skills`` (static for built-in groups, live for
        MCP servers, empty for a disconnected MCP stub) merged with the
        per-profile disabled set. ``supports_leaf_toggle`` is False for
        single-leaf groups (toggling the one leaf is redundant with the tool's
        own enable switch) so the UI can hide the section.
        """
        tool = self._tools.get(tool_id)
        if tool is None:
            raise KeyError(f"Tool '{tool_id}' is not registered")
        try:
            skills = tool.skills
        except Exception:  # noqa: BLE001
            logger.exception(f"leaves_for_profile: skills failed for '{tool_id}'")
            skills = []
        disabled = self._config.get_disabled_leaves(tool_id, profile)
        leaves = [
            {
                "leaf_name": s.id,
                "name": s.name,
                "description": s.description,
                "enabled": s.id not in disabled,
            }
            for s in skills
        ]
        return {
            "supports_leaf_toggle": len(leaves) > 1,
            "disconnected": bool(getattr(tool, "connection_error", None)),
            "leaves": leaves,
        }

    def set_profile_tool_leaf_enabled(
        self, profile: str, tool_id: str, leaf: str, enabled: bool,
    ) -> None:
        """Enable/disable a single sub-tool ("leaf") of a tool for ``profile``.

        Refuses ``hidden`` system tools (never user-facing). Unlike
        :meth:`set_profile_tool_enabled`, ``locked`` tools ARE allowed: the lock
        protects the group's presence, while per-leaf control is the whole point
        (e.g. make a locked ``system_file`` read-only). The leaf row is
        independent of the parent ``profile_tools`` flag, so the choice persists
        across a parent disable/re-enable.
        """
        tool = self._tools.get(tool_id)
        if tool is None:
            raise KeyError(f"Tool '{tool_id}' is not registered")
        if tool.hidden:
            raise ValueError(
                f"Tool '{tool_id}' is a hidden system tool and has no per-leaf controls."
            )
        self._config.set_leaf_enabled(tool_id, profile, leaf, enabled)


# ── singleton ──────────────────────────────────────────────────────────────

_instance: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    if _instance is None:
        raise RuntimeError(
            "ToolRegistry has not been initialized yet. Call set_tool_registry() first."
        )
    return _instance


def set_tool_registry(registry: ToolRegistry) -> None:
    global _instance
    _instance = registry
