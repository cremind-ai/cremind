"""Import step appliers — apply one blueprint component to the current profile.

Each ``apply_<step>`` writes design through the same storage/util APIs the live
app uses, so a blueprint always lands at the target's current schema. Steps run
in a fixed, dependency-ordered sequence enforced by the API (settings → persona
→ llm → tools → skills → events → listeners → finalize).

Skip semantics: skipping a step still applies its design; only *secret* values a
user declines are absent, surfacing as runtime errors later (a missing API key,
say) — exactly the spec's contract. The design is applied to the caller's
current profile (the user creates a fresh one beforehand if they don't want to
change an existing profile); aborting stops the import and clears staging but
leaves the profile intact.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.blueprint.plan import load_component
from app.blueprint.session import (
    STATE_ABORTED,
    STATE_DONE,
    STEP_APPLIED,
    STEP_SKIPPED,
    ImportSession,
)
from app.utils.logger import logger

# Names that must not be created as an imported profile: ``admin`` would grant
# admin tokens; a leading underscore is the hidden-profile convention.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9-][a-z0-9_-]*$")
_SKILL_EVENTS_CONTEXT_ID = "__skill_events__"


@dataclass
class Deps:
    registry: Any
    conversation_storage: Any
    config_storage: Any  # DynamicConfigStorage
    drop_profile_embeddings: Any = None


class StepError(Exception):
    """A step could not be applied (returned to the wizard as a retryable error)."""


def _result(applied: list | None = None, warnings: list | None = None,
            needs_attention: list | None = None) -> dict:
    return {
        "applied": applied or [],
        "warnings": warnings or [],
        "needs_attention": needs_attention or [],
    }


# ── profile ─────────────────────────────────────────────────────────────────


def validate_profile_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Profile name is required."
    if len(name) > 64:
        return "Profile name must be at most 64 characters."
    if name == "admin":
        return "The name 'admin' is reserved."
    if name.startswith("_"):
        return "Profile names cannot start with an underscore."
    if not _PROFILE_NAME_RE.match(name):
        return "Profile name may contain only lowercase letters, numbers, hyphens, and underscores."
    return None


async def create_target_profile(session: ImportSession, profile_name: str, deps: Deps) -> dict:
    from app.skills import initialize_profile_skills
    from app.utils.persona import ensure_persona_file

    err = validate_profile_name(profile_name)
    if err:
        raise StepError(err)
    if await deps.conversation_storage.profile_exists(profile_name):
        raise StepError(f"Profile '{profile_name}' already exists.")

    await deps.conversation_storage.create_profile(profile_name)
    ensure_persona_file(profile_name)
    if deps.registry is not None:
        try:
            await initialize_profile_skills(
                profile_name, deps.registry, loop=asyncio.get_running_loop()
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"[blueprint] skill init failed for new profile '{profile_name}'")
        deps.registry.on_profile_created(profile_name)

    session.target_profile = profile_name
    return _result(applied=[f"created profile '{profile_name}'"])


async def delete_target_profile(profile_name: str, deps: Deps) -> None:
    """Best-effort teardown of an imported profile (abort rollback)."""
    from app.skills import profile_skills_dir, teardown_profile_skills
    from app.tools.builtin.exec_shell_autostart import teardown_processes_for_dir

    try:
        await teardown_processes_for_dir(profile_skills_dir(profile_name), profile=profile_name)
    except Exception:  # noqa: BLE001
        logger.exception(f"[blueprint] listener teardown failed for '{profile_name}'")
    try:
        await deps.conversation_storage.delete_profile(profile_name)
    except Exception:  # noqa: BLE001
        logger.exception(f"[blueprint] delete_profile failed for '{profile_name}'")
    if deps.registry is not None:
        try:
            await teardown_profile_skills(
                profile_name, deps.registry, drop_embeddings=deps.drop_profile_embeddings
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"[blueprint] skill teardown failed for '{profile_name}'")


# ── settings ─────────────────────────────────────────────────────────────────


def apply_settings(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    from app.config.config_schema import all_keys

    data = load_component(session.payload_dir, "settings") or {}
    profile = session.target_profile
    schema_keys = set(all_keys())
    applied, warnings = [], []
    for key, value in (data.get("values") or {}).items():
        if key not in schema_keys:
            warnings.append(f"setting {key!r} not recognised by this build — skipped")
            continue
        deps.config_storage.set("user_config", key, str(value), profile=profile)
        applied.append(f"{key} = {value}")
    return _result(applied=applied, warnings=warnings)


# ── persona ─────────────────────────────────────────────────────────────────


def apply_persona(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    from app.utils.agent_name import write_agent_name
    from app.utils.persona import write_persona_file

    data = load_component(session.payload_dir, "persona") or {}
    profile = session.target_profile
    applied = []
    persona = data.get("persona_markdown")
    if persona is not None:
        write_persona_file(profile, persona)
        applied.append("persona")
    agent_name = (data.get("agent_name") or "").strip()
    if agent_name:
        write_agent_name(profile, agent_name)
        applied.append(f"agent name '{agent_name}'")
    return _result(applied=applied)


# ── llm ────────────────────────────────────────────────────────────────────


def apply_llm(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    data = load_component(session.payload_dir, "llm") or {}
    profile = session.target_profile
    cs = deps.config_storage
    applied, warnings, needs_attention = [], [], []

    # Non-secret provider settings (auth_method, etc.).
    supplied_secrets = inputs.get("secrets") or {}
    for provider in data.get("providers") or []:
        name = provider.get("name")
        for field, value in (provider.get("settings") or {}).items():
            cs.set("llm_config", f"{name}.{field}", str(value), is_secret=False, profile=profile)
        for field in provider.get("required_secrets") or []:
            key = f"{name}.{field}"
            if key not in supplied_secrets:
                needs_attention.append(
                    f"LLM provider {name!r} needs {field!r} — set it in Settings → LLM later"
                )

    # custom provider registry (non-secret JSON).
    custom = data.get("custom_providers") or {}
    if custom:
        cs.set("llm_config", "custom_providers", json.dumps(custom), is_secret=False, profile=profile)

    # model groups.
    for group, mg in (data.get("model_groups") or {}).items():
        if mg.get("model"):
            cs.set("llm_config", f"model_group.{group}", str(mg["model"]), profile=profile)
            applied.append(f"model_group.{group} = {mg['model']}")
        if mg.get("reasoning_effort"):
            cs.set("llm_config", f"model_group.{group}.reasoning_effort", str(mg["reasoning_effort"]), profile=profile)
        if group == "vision" and "enabled" in mg:
            cs.set("llm_config", "model_group.vision.enabled", "true" if mg["enabled"] else "false", profile=profile)

    if data.get("default_provider"):
        cs.set("llm_config", "default_provider", str(data["default_provider"]), profile=profile)

    # Secrets the user supplied at this step (applied then discarded).
    for key, value in supplied_secrets.items():
        cs.set("llm_config", str(key), str(value), is_secret=True, profile=profile)
        applied.append(f"secret {key}")

    return _result(applied=applied, warnings=warnings, needs_attention=needs_attention)


# ── tools (builtin + a2a/mcp) ─────────────────────────────────────────────────


def _apply_tool_config_bundle(mgr, tool_id: str, profile: str, entry: dict, secrets: dict) -> None:
    cfg = entry.get("config") or {}
    args = cfg.get("arg") or {}
    if args:
        mgr.set_arguments(tool_id, profile, args)
    for key, value in (cfg.get("llm") or {}).items():
        mgr.set_llm_param(tool_id, profile, key, value)
    for key, value in (cfg.get("meta") or {}).items():
        mgr.set_meta(tool_id, profile, key, value)
    for key, value in (entry.get("variables") or {}).items():
        mgr.set_variable(tool_id, profile, key, str(value), is_secret=False)
    for key, value in (secrets or {}).items():
        mgr.set_variable(tool_id, profile, str(key), str(value), is_secret=True)
    for leaf in entry.get("disabled_leaves") or []:
        mgr.set_leaf_enabled(tool_id, profile, leaf, False)


def apply_tools(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    from app.tools.config_manager import ToolConfigManager

    data = load_component(session.payload_dir, "tools") or {}
    profile = session.target_profile
    ts = deps.registry.storage if deps.registry is not None else _tool_storage()
    mgr = ToolConfigManager(ts)
    per_tool_secrets = inputs.get("secrets") or {}
    applied, warnings = [], []

    for tool in data.get("tools") or []:
        tool_id = tool.get("tool_id")
        kind = tool.get("kind")
        if kind in ("a2a", "mcp"):
            enabled = tool.get("enabled")
            if enabled is not None:
                if ts.get_tool(tool_id) is not None:
                    ts.set_profile_tool(profile, tool_id, bool(enabled))
                    applied.append(f"{tool_id} enabled={enabled}")
                else:
                    warnings.append(
                        f"tool {tool_id!r} ({kind}) is not installed here — enable state not applied"
                    )
        _apply_tool_config_bundle(mgr, tool_id, profile, tool, per_tool_secrets.get(tool_id, {}))
        applied.append(f"configured {tool_id}")

    return _result(applied=applied, warnings=warnings)


def _tool_storage():
    from app.storage.tool_storage import get_tool_storage

    return get_tool_storage()


# ── skills ─────────────────────────────────────────────────────────────────


async def apply_skills(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    from app.skills.importer import install_single_skill
    from app.skills.sync import resync_profile_skills
    from app.tools.config_manager import ToolConfigManager

    data = load_component(session.payload_dir, "skills") or {}
    profile = session.target_profile
    payload_skills = session.payload_dir / "skills"
    conflicts = inputs.get("conflicts") or {}
    per_skill_secrets = inputs.get("secrets") or {}
    applied, warnings = [], []

    entries = data.get("skills") or []

    # 1. Install bundled skill dirs (honouring per-skill conflict decisions).
    for e in entries:
        if not e.get("bundled"):
            continue
        dir_name = e.get("dir")
        src = payload_skills / dir_name
        if not src.is_dir():
            warnings.append(f"bundled skill {dir_name!r} missing from archive — skipped")
            continue
        decision = conflicts.get(dir_name) or conflicts.get(e.get("slug")) or "skip"
        res = install_single_skill(src, profile, on_conflict=decision)
        if res["installed"]:
            applied.append(f"installed skill {res['dir']}")
        elif res["action"] == "keep_local":
            warnings.append(f"kept local skill {res['dir']} ({res['reason']})")
        else:
            warnings.append(f"skill {res['dir']}: {res['reason']}")

    # 2. Resync FIRST so the registry registers the (re-keyed) skill tool rows.
    #    ``tool_configs.tool_id`` has a foreign key to ``tools.tool_id``, so the
    #    skill's tool row must exist before its config rows are written.
    if deps.registry is not None:
        await resync_profile_skills(profile, deps.registry)

    # 3. Re-key + write each skill's tool_configs under this profile's tool_id.
    ts = deps.registry.storage if deps.registry is not None else _tool_storage()
    mgr = ToolConfigManager(ts)
    for e in entries:
        tool_id = f"{profile}__{e['slug']}"
        _apply_tool_config_bundle(mgr, tool_id, profile, e, per_skill_secrets.get(e["slug"], {}))

    # 4. Resync again so each skill's scripts/.env is re-materialized from the
    #    variables just persisted (the first resync wrote an empty .env).
    if deps.registry is not None:
        await resync_profile_skills(profile, deps.registry)

    return _result(applied=applied, warnings=warnings)


# ── events ─────────────────────────────────────────────────────────────────


async def apply_events(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    import os
    from datetime import datetime

    from app.backup.paths import build_path_map, relocate_path
    from app.blueprint.manifest import BlueprintManifest
    from app.calendar import feature as calendar_feature
    from app.calendar import recurrence as R
    from app.calendar.provider import InternalCalendarProvider
    from app.storage.event_subscription_storage import EventSubscriptionStorage
    from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage

    data = load_component(session.payload_dir, "events") or {}
    profile = session.target_profile
    applied, warnings, needs_attention = [], [], []

    # Feature flag.
    calendar_feature.set_enabled(profile, bool(data.get("calendar_schedule_enabled", True)))

    # Schedules — recompute next_fire_at; skip wholly-past events.
    now = datetime.now().replace(microsecond=0)
    schedules = data.get("schedule") or []
    if schedules:
        conv = await deps.conversation_storage.get_or_create_conversation(
            profile=profile, context_id="__schedule__"
        )
        provider = InternalCalendarProvider()
        for row in schedules:
            rrule = row.get("rrule")
            dtstart = row.get("dtstart") or ""
            until = row.get("recurrence_end_value") if row.get("recurrence_end_type") == "until" else None
            try:
                if rrule:
                    seed = R.first_occurrence_on_or_after(rrule=rrule, dtstart=dtstart, moment=now, until=until)
                else:
                    start = R.parse_local(dtstart)
                    seed = start if start >= now else None
            except Exception:  # noqa: BLE001
                seed = None
            if seed is None:
                needs_attention.append(f"schedule '{row.get('title')}' already elapsed — not imported")
                continue
            provider.create_event(
                profile=profile,
                conversation_id=conv["id"],
                title=row.get("title") or "",
                action=row.get("action") or "",
                source=row.get("source") or "manual",
                schedule_kind=row.get("schedule_kind") or ("recurrence" if rrule else "instant"),
                dtstart=dtstart,
                duration_minutes=int(row.get("duration_minutes") or 30),
                all_day=bool(row.get("all_day")),
                rrule=rrule,
                recurrence_end_type=row.get("recurrence_end_type"),
                recurrence_end_value=row.get("recurrence_end_value"),
                timezone=row.get("timezone"),
            )
            applied.append(f"schedule '{row.get('title')}'")

    # File watchers — relocate root_path (or use a user-supplied override);
    # insert + arm even if the path is missing (skip still applies the design).
    watchers = data.get("file_watcher") or []
    if watchers:
        from app.events import get_file_watcher_manager

        manifest = BlueprintManifest.from_dict(session.manifest) if session.manifest else None
        pm = build_path_map(manifest, _system_dir(), os.path.expanduser("~")) if manifest else None
        overrides = inputs.get("watcher_paths") or {}
        fw_store = FileWatcherSubscriptionStorage()
        for w in watchers:
            name = w.get("name") or "watcher"
            root = overrides.get(name)
            if not root:
                root = w.get("root_path") or ""
                if pm is not None:
                    relocated, changed, _abs = relocate_path(pm, root)
                    if changed:
                        root = relocated
            conv = await deps.conversation_storage.create_conversation(
                profile=profile, title=f"File Watcher: {name}"
            )
            fw_row = fw_store.insert(
                conversation_id=conv["id"],
                profile=profile,
                name=name,
                root_path=root,
                recursive=bool(w.get("recursive", True)),
                target_kind=w.get("target_kind") or "any",
                event_types=w.get("event_types") or "created,modified,deleted,moved",
                extensions=w.get("extensions") or "",
                action=w.get("action") or "",
            )
            armed = False
            if os.path.isdir(root):
                try:
                    armed = get_file_watcher_manager().arm(fw_row)
                except Exception:  # noqa: BLE001
                    logger.exception("[blueprint] file watcher arm failed")
            if armed:
                applied.append(f"file watcher '{name}'")
            else:
                needs_attention.append(
                    f"file watcher '{name}' path {root!r} does not exist here — set it in Events later"
                )

    # Skill events — bind to a per-profile host conversation and insert.
    skill_events = data.get("skill_event") or []
    if skill_events:
        host = await deps.conversation_storage.get_or_create_conversation(
            profile=profile, context_id=_SKILL_EVENTS_CONTEXT_ID
        )
        se_store = EventSubscriptionStorage()
        for e in skill_events:
            skill_name = e.get("skill_name") or e.get("skill_slug") or ""
            se_store.insert(
                conversation_id=host["id"],
                profile=profile,
                skill_name=skill_name,
                event_type=e.get("event_type") or "",
                action=e.get("action") or "",
            )
            applied.append(f"skill event {skill_name}/{e.get('event_type')}")

    return _result(applied=applied, warnings=warnings, needs_attention=needs_attention)


# ── listeners ─────────────────────────────────────────────────────────────────


async def apply_listeners(session: ImportSession, inputs: dict, deps: Deps) -> dict:
    from pathlib import Path as _P

    from app.skills.sync import profile_skills_dir
    from app.storage import get_autostart_storage
    from app.tools.builtin.exec_shell_autostart import spawn_from_autostart

    data = load_component(session.payload_dir, "listeners") or {}
    profile = session.target_profile
    confirmed = set(inputs.get("confirmed") or [])
    autostart = get_autostart_storage()
    applied, warnings, needs_attention = [], [], []

    for li in data.get("listeners") or []:
        skill_dir = li.get("skill_dir")
        source_dir = profile_skills_dir(profile) / skill_dir
        script = _P(source_dir) / "scripts" / "event_listener.py"
        command = f'uv run "{script}"'
        working_dir = str(_P(source_dir) / "scripts")
        row = autostart.find_duplicate(profile, command, working_dir=working_dir)
        if row is None:
            row = autostart.insert(
                profile=profile, command=command, working_dir=working_dir,
                is_pty=bool(li.get("is_pty")),
            )
        if skill_dir in confirmed:
            process_id, error = await spawn_from_autostart(row)
            if error:
                autostart.set_error(row["id"], error)
                needs_attention.append(f"listener {skill_dir!r} failed to start: {error}")
            else:
                autostart.clear_error(row["id"])
                applied.append(f"started listener {skill_dir}")
        else:
            applied.append(f"registered listener {skill_dir} (starts on next restart)")

    return _result(applied=applied, warnings=warnings, needs_attention=needs_attention)


# ── finalize / abort ───────────────────────────────────────────────────────────


def finalize(session: ImportSession, deps: Deps) -> dict:
    """Assemble the final report from step results and clean the payload up."""
    import shutil

    applied, skipped, needs_attention = [], [], []
    for s in session.steps:
        key = s["key"]
        res = s.get("result") or {}
        if s.get("status") == STEP_SKIPPED:
            skipped.append(key)
        applied.extend(f"{key}: {a}" for a in res.get("applied", []))
        needs_attention.extend(f"{key}: {n}" for n in res.get("needs_attention", []))
        needs_attention.extend(f"{key}: {w}" for w in res.get("warnings", []))

    report = {
        "profile": session.target_profile,
        "applied": applied,
        "skipped": skipped,
        "needs_attention": needs_attention,
        "warnings": [w.get("message") for w in session.warnings],
    }
    session.report = report
    session.state = STATE_DONE
    session.save()

    # Remove the extracted payload + raw upload; keep session.json for the report.
    try:
        shutil.rmtree(session.payload_dir, ignore_errors=True)
        (session.dir / "upload.blueprint").unlink(missing_ok=True)
    except OSError:
        pass
    return report


async def abort(session: ImportSession, deps: Deps, *, delete_profile: bool) -> None:
    import shutil

    if delete_profile and session.target_profile:
        await delete_target_profile(session.target_profile, deps)
    session.state = STATE_ABORTED
    session.save()
    shutil.rmtree(session.dir, ignore_errors=True)


def _system_dir() -> str:
    from app.config.settings import BaseConfig

    return BaseConfig.CREMIND_SYSTEM_DIR


# ── dispatch ─────────────────────────────────────────────────────────────────

# Sync appliers vs async appliers (profile/skills/events/listeners need await).
SYNC_STEPS = {"settings": apply_settings, "persona": apply_persona,
              "llm": apply_llm, "tools": apply_tools}
ASYNC_STEPS = {"skills": apply_skills, "events": apply_events, "listeners": apply_listeners}


async def apply_step(session: ImportSession, key: str, inputs: dict, deps: Deps) -> dict:
    """Apply a single step by key. ``profile`` is handled by the API directly."""
    if key in SYNC_STEPS:
        return SYNC_STEPS[key](session, inputs, deps)
    if key in ASYNC_STEPS:
        return await ASYNC_STEPS[key](session, inputs, deps)
    raise StepError(f"Unknown step: {key!r}")


__all__ = [
    "Deps",
    "StepError",
    "abort",
    "apply_step",
    "create_target_profile",
    "delete_target_profile",
    "finalize",
    "validate_profile_name",
]
