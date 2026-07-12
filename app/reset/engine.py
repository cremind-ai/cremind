"""Per-profile "clean data" engine.

Maps each component in the shared vocabulary (:mod:`app.reset.components`) to the
existing teardown call for that data, and runs the requested subset in a fixed
**dependents-before-parents** order. Every component is best-effort: a failure is
recorded in ``errors`` and the rest still run, so one wedged deletion can't strand a
factory reset half-done.

The engine reuses the same teardown the rest of the app already uses (conversation
dependent-cleanup, event-run cascades, channel stop, config wipes, skill/persona reset,
document + embedding pruning) rather than issuing its own SQL — so behavior stays
consistent with the single-item delete paths.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
from typing import Any

from app.reset.deps import Deps
from app.utils.logger import logger


# ── filesystem helpers ──────────────────────────────────────────────────────

def _force_writable(func, path, _exc_info):
    """rmtree onerror handler: clear the read-only bit (Windows) and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:  # noqa: BLE001
        logger.debug(f"clean: could not remove {path}", exc_info=True)


def _rmtree(path: str | None) -> bool:
    """Recursively remove a directory tree; returns True if it's gone afterwards."""
    if not path or not os.path.exists(path):
        return False
    try:
        shutil.rmtree(path, onerror=_force_writable)
    except Exception:  # noqa: BLE001
        logger.exception(f"clean: rmtree failed for {path}")
    return not os.path.exists(path)


# ── per-component handlers (each returns a count or a small detail dict) ──────

async def _clean_skill_events(profile: str, deps: Deps) -> int:
    from app.storage import get_event_subscription_storage
    from app.events.run_lifecycle import delete_runs_for_subscription, SKILL
    store = get_event_subscription_storage()
    n = 0
    for sub in store.list_by_profile(profile):
        await delete_runs_for_subscription(SKILL, sub["id"], profile)
        if store.delete(sub["id"]):
            n += 1
    return n


async def _clean_file_watchers(profile: str, deps: Deps) -> int:
    from app.storage import get_file_watcher_storage
    from app.events.run_lifecycle import delete_runs_for_subscription, FILE_WATCHER
    store = get_file_watcher_storage()
    try:
        from app.events.file_watcher_manager import get_file_watcher_manager
        mgr = get_file_watcher_manager()
    except Exception:  # noqa: BLE001
        mgr = None
    n = 0
    for sub in store.list_by_profile(profile):
        await delete_runs_for_subscription(FILE_WATCHER, sub["id"], profile)
        # Delete the row BEFORE disarming: the manager re-queries peers on the
        # same root and keeps the observer alive if it still sees this row.
        store.delete(sub["id"])
        if mgr is not None:
            try:
                mgr.disarm(sub)
            except Exception:  # noqa: BLE001
                logger.debug("clean: file-watcher disarm failed", exc_info=True)
        n += 1
    return n


async def _clean_schedules(profile: str, deps: Deps) -> int:
    from app.storage import get_schedule_event_storage
    from app.events.run_lifecycle import delete_runs_for_subscription, SCHEDULE
    store = get_schedule_event_storage()
    try:
        from app.events.schedule_manager import get_schedule_manager
        mgr = get_schedule_manager()
    except Exception:  # noqa: BLE001
        mgr = None
    n = 0
    for sub in store.list_by_profile(profile):
        await delete_runs_for_subscription(SCHEDULE, sub["id"], profile)
        if mgr is not None:
            try:
                mgr.remove(sub["id"])
            except Exception:  # noqa: BLE001
                logger.debug("clean: schedule remove failed", exc_info=True)
        if store.delete(sub["id"]):
            n += 1
    return n


async def _clean_event_runs(profile: str, deps: Deps) -> int:
    """Clear run history for the profile's rules without deleting the rules.

    No-op when the rules themselves were already deleted (working/factory delete
    schedules/watchers/skill-events first, cascading their runs), so this stays
    idempotent alongside the subscription components.
    """
    from app.storage import (
        get_event_subscription_storage, get_file_watcher_storage,
        get_schedule_event_storage,
    )
    from app.events.run_lifecycle import (
        delete_runs_for_subscription, SKILL, FILE_WATCHER, SCHEDULE,
    )
    total = 0
    for store, kind in (
        (get_event_subscription_storage(), SKILL),
        (get_file_watcher_storage(), FILE_WATCHER),
        (get_schedule_event_storage(), SCHEDULE),
    ):
        for sub in store.list_by_profile(profile):
            total += await delete_runs_for_subscription(kind, sub["id"], profile)
    return total


async def _clean_conversations(profile: str, deps: Deps) -> int:
    from app.reset._conversations import delete_all_chat
    return await delete_all_chat(deps.conversation_storage, profile)


async def _clean_channels(profile: str, deps: Deps) -> int:
    """Delete every external channel, keeping the auto 'main' channel."""
    from app.channels import get_channel_registry
    try:
        reg = get_channel_registry()
    except Exception:  # noqa: BLE001
        reg = None
    n = 0
    for ch in await deps.conversation_storage.list_channels(profile):
        if ch.get("channel_type") == "main":
            continue
        cid = ch["id"]
        if reg is not None:
            try:
                await reg.stop_for_channel(cid)
            except Exception:  # noqa: BLE001
                logger.exception(f"clean: channel stop failed for {cid}")
        if await deps.conversation_storage.delete_channel(cid):
            n += 1
    return n


async def _clean_usage(profile: str, deps: Deps) -> int:
    from app.storage import get_usage_storage
    return await get_usage_storage().delete_for_profile(profile)


async def _clean_memory(profile: str, deps: Deps) -> int:
    from app.storage import get_memory_storage
    n = await get_memory_storage().clear(profile)
    # When embedding is ON, long-term memory lives in the vector store instead of
    # the DB — drop that profile's memory collection too.
    if deps.drop_profile_embeddings is not None:
        try:
            await asyncio.to_thread(deps.drop_profile_embeddings, profile)
        except Exception:  # noqa: BLE001
            logger.debug("clean: drop_profile_embeddings failed", exc_info=True)
    return n


async def _clean_llm_config(profile: str, deps: Deps) -> int:
    return deps.config_storage.delete_by_prefix("llm_config", "", profile=profile)


async def _clean_app_settings(profile: str, deps: Deps) -> int:
    return deps.config_storage.delete_by_prefix("user_config", "", profile=profile)


async def _clean_tool_configs(profile: str, deps: Deps) -> dict[str, int]:
    """Remove the profile's tool/MCP customization: per-tool config + variables,
    enable-state, user-registered a2a/mcp tools, and autostart processes."""
    from app.storage import get_tool_storage, get_autostart_storage
    ts = get_tool_storage()
    removed = {"tool_configs": 0, "profile_tools": 0, "tools": 0, "autostart": 0}

    # per-tool config rows (arg/variable/llm/meta scopes, incl. skill env vars)
    tool_ids = {row["tool_id"] for row in ts.list_config_keys(profile)}
    for tid in tool_ids:
        removed["tool_configs"] += ts.delete_all_configs(profile=profile, tool_id=tid)

    # per-profile enable/visibility state
    for tid in list(ts.list_profile_tools(profile).keys()):
        if ts.delete_profile_tool(profile, tid):
            removed["profile_tools"] += 1

    # user-registered a2a/mcp tools owned by this profile → drop from live
    # registry (also deletes the DB row); skill rows are handled by _clean_skills.
    reg = deps.registry
    for t in ts.list_tools():
        if t.get("owner_profile") != profile or t.get("tool_type") not in ("a2a", "mcp"):
            continue
        tid = t["tool_id"]
        dropped = False
        if reg is not None:
            try:
                dropped = bool(await reg.unregister(tid))
            except Exception:  # noqa: BLE001
                logger.exception(f"clean: registry unregister failed for {tid}")
        if not dropped:
            dropped = ts.delete_tool(tid)
        if dropped:
            removed["tools"] += 1

    # autostart: stop the profile's running autostart instances, then drop the
    # autostart_processes rows so they don't relaunch on boot. (Ad-hoc runtime
    # processes are handled separately by the ``processes`` component.)
    try:
        from app.tools.builtin.exec_shell import stop_processes_for_profile
        await stop_processes_for_profile(profile, autostart=True)
    except Exception:  # noqa: BLE001
        logger.exception("clean: autostart process stop failed")
    astore = get_autostart_storage()
    for row in astore.list(profile):
        if astore.delete(row["id"], profile):
            removed["autostart"] += 1
    return removed


async def _clean_processes(profile: str, deps: Deps) -> int:
    """Kill the profile's ad-hoc runtime processes (background shells the agent
    spawned via exec_shell). Registered autostarts are config — left running
    here and handled by the ``tool_configs`` component on a factory reset."""
    from app.tools.builtin.exec_shell import stop_processes_for_profile
    return await stop_processes_for_profile(profile, autostart=False)


async def _clean_oauth_tokens(profile: str, deps: Deps) -> int:
    from app.utils.client_storage import get_auth_client_storage
    return get_auth_client_storage().delete_tokens_for_profile(profile)


async def _clean_skills(profile: str, deps: Deps) -> dict[str, Any]:
    """Identity → defaults: reset PERSONA.md to the template and every built-in
    skill to its shipped default, and remove user-added skills."""
    detail: dict[str, Any] = {
        "persona_reset": False, "builtins_reset": 0, "user_skills_removed": 0,
    }

    # persona: unlink then re-copy the shipped template
    try:
        from app.utils.persona import ensure_persona_file
        path = ensure_persona_file(profile)
        await asyncio.to_thread(_unlink, path)
        ensure_persona_file(profile)
        detail["persona_reset"] = True
    except Exception:  # noqa: BLE001
        logger.exception("clean: persona reset failed")

    # skills: drop user skills, re-copy every shipped built-in, then resync registry
    try:
        from app.skills.sync import (
            profile_skills_dir, is_builtin_skill_dir, builtin_skill_dir_names,
            reset_builtin_skill, delete_profile_skill, resync_profile_skills,
        )
        skills_dir = profile_skills_dir(profile)
        installed = (
            [d.name for d in skills_dir.iterdir() if d.is_dir()]
            if skills_dir.exists() else []
        )
        for name in installed:
            if not is_builtin_skill_dir(name):
                if await asyncio.to_thread(delete_profile_skill, profile, name):
                    detail["user_skills_removed"] += 1
        for name in builtin_skill_dir_names():
            try:
                await asyncio.to_thread(reset_builtin_skill, profile, name)
                detail["builtins_reset"] += 1
            except Exception:  # noqa: BLE001
                logger.debug(f"clean: reset_builtin_skill {name} failed", exc_info=True)
        if deps.registry is not None:
            await resync_profile_skills(profile, deps.registry)
    except Exception:  # noqa: BLE001
        logger.exception("clean: skills reset failed")
    return detail


async def _clean_documents(profile: str, deps: Deps) -> dict[str, bool]:
    ds = deps.document_service
    detail = {"removed": False}
    if ds is None:
        return detail
    try:
        pdir = ds.profile_dir(profile)
        detail["removed"] = await asyncio.to_thread(_rmtree, str(pdir))
    except Exception:  # noqa: BLE001
        logger.exception("clean: documents dir removal failed")
    try:
        await asyncio.to_thread(ds.prune_scope, profile)
    except Exception:  # noqa: BLE001
        logger.exception("clean: document embedding prune failed")
    return detail


async def _clean_browser_login(profile: str, deps: Deps) -> dict[str, bool]:
    try:
        from app.tools.builtin.browser import _profile_default_user_data_dir, _sessions
    except Exception:  # noqa: BLE001
        return {"removed": False}
    try:
        session = _sessions.get(profile)
        if session is not None:
            try:
                await session.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("clean: browser disconnect failed", exc_info=True)
            _sessions.pop(profile, None)
    except Exception:  # noqa: BLE001
        logger.exception("clean: browser session close failed")
    removed = await asyncio.to_thread(_rmtree, _profile_default_user_data_dir(profile))
    return {"removed": removed}


async def _clean_uploads(profile: str, deps: Deps) -> dict[str, bool]:
    from app.utils.uploads_tmp import uploads_tmp_root
    removed = await asyncio.to_thread(_rmtree, uploads_tmp_root(profile))
    return {"removed": removed}


async def _clean_plans(profile: str, deps: Deps) -> dict[str, bool]:
    from app.utils.plans_dir import plans_root
    removed = await asyncio.to_thread(_rmtree, plans_root(profile))
    return {"removed": removed}


def _unlink(path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.debug(f"clean: unlink failed for {path}", exc_info=True)


# Canonical execution order: dependents (runs, subscriptions) → conversations →
# channels → per-profile config/credentials/content → on-disk scratch trees.
_ORDER: tuple[tuple[str, Any], ...] = (
    ("processes", _clean_processes),
    ("skill_events", _clean_skill_events),
    ("file_watchers", _clean_file_watchers),
    ("schedules", _clean_schedules),
    ("event_runs", _clean_event_runs),
    ("conversations", _clean_conversations),
    ("channels", _clean_channels),
    ("usage", _clean_usage),
    ("memory", _clean_memory),
    ("tool_configs", _clean_tool_configs),
    ("llm_config", _clean_llm_config),
    ("app_settings", _clean_app_settings),
    ("oauth_tokens", _clean_oauth_tokens),
    ("skills", _clean_skills),
    ("documents", _clean_documents),
    ("browser_login", _clean_browser_login),
    ("uploads", _clean_uploads),
    ("plans", _clean_plans),
)


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return sum(_count(v) for v in value.values())
    return 0


def _publish_changes(profile: str, ran: list[str]) -> None:
    """Fire the admin/list SSE change-events so open UI views live-update."""
    def _safe(fn) -> None:
        try:
            fn(profile)
        except Exception:  # noqa: BLE001
            logger.debug("clean: change-event publish failed", exc_info=True)

    if "conversations" in ran:
        from app.events.conversations_list_bus import publish_conversations_changed
        _safe(publish_conversations_changed)
    if "skill_events" in ran:
        from app.api.events import publish_skill_events_admin_changed
        _safe(publish_skill_events_admin_changed)
    if "file_watchers" in ran:
        from app.api.file_watchers import publish_file_watchers_admin_changed
        _safe(publish_file_watchers_admin_changed)
    if "schedules" in ran:
        from app.api.calendar import publish_schedule_events_admin_changed
        _safe(publish_schedule_events_admin_changed)
    if "event_runs" in ran or "skill_events" in ran or "file_watchers" in ran or "schedules" in ran:
        from app.events.event_runs_admin_bus import publish_event_runs_changed
        _safe(publish_event_runs_changed)
    if any(k in ran for k in ("llm_config", "app_settings", "tool_configs", "skills", "oauth_tokens", "channels")):
        from app.events.settings_state_bus import publish_settings_state_changed
        _safe(publish_settings_state_changed)


async def run_clean(profile: str, components: set[str], deps: Deps) -> dict[str, Any]:
    """Clean the requested components for ``profile``; return a per-component report.

    Returns ``{"cleaned": {component: count|detail}, "errors": {component: msg},
    "total": int, "components": [ran...]}``. Never raises for a per-component
    failure — those land in ``errors``.
    """
    cleaned: dict[str, Any] = {}
    errors: dict[str, str] = {}
    ran: list[str] = []
    for key, handler in _ORDER:
        if key not in components:
            continue
        try:
            cleaned[key] = await handler(profile, deps)
            ran.append(key)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"clean: component '{key}' failed for profile={profile}")
            errors[key] = str(exc)

    # Best-effort: drop in-memory reasoning contexts once conversations are gone.
    if "conversations" in ran:
        try:
            from app.agent.context_store import ReasoningContextStore
            ReasoningContextStore().clear_all_contexts()
        except Exception:  # noqa: BLE001
            logger.debug("clean: clear_all_contexts failed", exc_info=True)

    _publish_changes(profile, ran)
    return {
        "cleaned": cleaned,
        "errors": errors,
        "total": sum(_count(v) for v in cleaned.values()),
        "components": ran,
    }
