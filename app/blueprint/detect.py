"""Export checklist detection: which components a profile can contribute.

Only components the creator actually *customized* appear in the export UI — a
setting still at its default, a persona identical to the template, a profile
with no configured tools all stay off the list. :func:`collect_exportable`
returns, per component, an ``available`` flag plus a compact summary the UI
renders as one checkbox row.

Detection is intentionally lightweight (existence queries + a template compare),
distinct from the full document builders in :mod:`app.blueprint.components` that
run at export time; the two share only the skill-entry helper.
"""

from __future__ import annotations

from typing import Any


def _persona_customized(profile: str) -> tuple[bool, dict]:
    from pathlib import Path

    from app.utils.agent_name import default_agent_name, read_agent_name
    from app.utils.persona import PERSONA_FILENAME, read_persona_file

    persona = read_persona_file(profile)
    template_path = Path(__file__).resolve().parent.parent / "templates" / PERSONA_FILENAME
    template = ""
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError:
        template = ""

    agent_name = read_agent_name(profile)
    name_changed = agent_name != default_agent_name(profile)
    persona_changed = _norm(persona) != _norm(template)

    return (persona_changed or name_changed), {
        "agent_name": agent_name,
        "persona_chars": len(persona),
    }


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


def collect_exportable(profile: str) -> dict[str, Any]:
    """Return ``{"profile", "components": {<key>: {available, ...}}}``."""
    from app.blueprint.components import (
        _secret_map,
        collect_skill_entries,
    )
    from app.calendar.feature import is_enabled
    from app.storage import get_autostart_storage
    from app.storage.dynamic_config_storage import DynamicConfigStorage
    from app.storage.event_subscription_storage import EventSubscriptionStorage
    from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage
    from app.storage.schedule_event_storage import ScheduleEventSubscriptionStorage
    from app.storage.tool_storage import get_tool_storage
    from app.tools.ids import slugify

    from app.blueprint.components import build_listeners_doc, build_tools_doc

    ts = get_tool_storage()
    cs = DynamicConfigStorage()
    secret_map = _secret_map(profile)
    components: dict[str, Any] = {}

    # persona
    available, summary = _persona_customized(profile)
    components["persona"] = {"available": available, "summary": summary}

    # tools — reuse the export builder so the checklist matches exactly.
    tools_doc, _req = build_tools_doc(profile, secret_map=secret_map)
    tool_items = []
    for t in tools_doc["data"]["tools"]:
        defn = t.get("definition") or {}
        # Built-ins carry no ``definition`` block but do have a friendly
        # name/description in the tools table — surface it so every tool
        # displays richly (like skills), falling back to the raw id.
        row = ts.get_tool(t["tool_id"]) or {}
        # Count the customized (non-secret) settings so the row can show a
        # configuration footprint — the tool analogue of a skill's size hint.
        cfg = t.get("config") or {}
        settings_count = (
            len(cfg.get("arg") or {})
            + len(cfg.get("meta") or {})
            + len(t.get("variables") or {})
        )
        tool_items.append(
            {
                "tool_id": t["tool_id"],
                "name": defn.get("name") or row.get("name") or t["tool_id"],
                "kind": t["kind"],  # "builtin" | "a2a" | "mcp"
                "description": defn.get("description") or row.get("description"),
                "source": defn.get("source"),
                "enabled": t.get("enabled"),
                "settings_count": settings_count,
                "secret_variables": t.get("secret_variables") or [],
                "has_secret_variables": bool(t.get("secret_variables")),
                "disabled_leaves": len(t.get("disabled_leaves") or []),
            }
        )
    components["tools"] = {
        "available": bool(tool_items),
        "count": len(tool_items),
        "items": tool_items,
    }

    # llm
    llm_keys = cs.list_keys("llm_config", profile)
    llm_available = bool(llm_keys)
    llm_vals = cs.get_all("llm_config", include_secrets=False, profile=profile) if llm_available else {}
    configured_providers = sorted(
        {
            k.split(".", 1)[0]
            for k in llm_vals
            if "." in k and not k.startswith("model_group") and not k.startswith("custom:")
        }
    )
    custom_provider_slugs: list[str] = []
    raw_custom = llm_vals.get("custom_providers")
    if raw_custom and raw_custom != "***":
        import json

        try:
            parsed = json.loads(raw_custom) if isinstance(raw_custom, str) else raw_custom
            if isinstance(parsed, dict):
                custom_provider_slugs = sorted(parsed.keys())
        except (ValueError, TypeError):
            pass
    components["llm"] = {
        "available": llm_available,
        "summary": {
            "default_provider": llm_vals.get("default_provider"),
            "configured_providers": configured_providers,
            "custom_providers": custom_provider_slugs,
            "model_groups": {
                g: llm_vals.get(f"model_group.{g}")
                for g in ("high", "vision", "low")
                if llm_vals.get(f"model_group.{g}")
            },
        },
    }

    # settings
    settings_vals = cs.get_all("user_config", profile=profile)
    components["settings"] = {
        "available": bool(settings_vals),
        "count": len(settings_vals),
        "keys": sorted(settings_vals.keys()),
    }

    # events (need counts first so skills can reference them)
    schedule_rows = [
        r
        for r in ScheduleEventSubscriptionStorage().list_by_profile(profile)
        if not r.get("external_provider") and r.get("status") != "cancelled"
    ]
    excluded_mirrors = sum(
        1
        for r in ScheduleEventSubscriptionStorage().list_by_profile(profile)
        if r.get("external_provider")
    )
    watcher_rows = FileWatcherSubscriptionStorage().list_by_profile(profile)
    skill_event_rows = EventSubscriptionStorage().list_by_profile(profile)

    # listeners
    listeners_doc, _lreq = build_listeners_doc(profile)
    listener_items = listeners_doc["data"]["listeners"]

    referenced_slugs = {slugify(r["skill_name"]) for r in skill_event_rows}
    referenced_dirs = {li["skill_dir"] for li in listener_items}

    # skills — user skills always; built-ins only if configured or referenced.
    all_entries = collect_skill_entries(profile, secret_map)
    skill_items: list[dict] = []
    for e in all_entries:
        show = (
            not e["builtin"]
            or e["_configured"]
            or e["slug"] in referenced_slugs
            or e["dir"] in referenced_dirs
        )
        if not show:
            continue
        skill_items.append(
            {
                "name": e["name"],
                "slug": e["slug"],
                "dir": e["dir"],
                "builtin": e["builtin"],
                "bundled": e["bundled"],
                "secret_variables": e["secret_variables"],
                "has_listener": e["has_listener"],
                "approx_bytes": e["approx_bytes"],
            }
        )
    components["skills"] = {"available": bool(skill_items), "items": skill_items}

    components["events"] = {
        "available": bool(schedule_rows or watcher_rows or skill_event_rows),
        "counts": {
            "schedule": len(schedule_rows),
            "file_watcher": len(watcher_rows),
            "skill_event": len(skill_event_rows),
        },
        "excluded": {"external_mirrors": excluded_mirrors},
        "items": {
            "schedule": [{"title": r["title"]} for r in schedule_rows],
            "file_watcher": [
                {"name": r["name"], "root_path": r["root_path"]} for r in watcher_rows
            ],
            "skill_event": [
                {"skill_slug": slugify(r["skill_name"]), "event_type": r["event_type"]}
                for r in skill_event_rows
            ],
        },
    }

    components["listeners"] = {
        "available": bool(listener_items),
        "items": [{"skill_dir": li["skill_dir"]} for li in listener_items],
        "skipped_non_skill": listeners_doc["data"].get("_skipped_non_skill", 0),
    }

    return {"profile": profile, "components": components}


__all__ = ["collect_exportable"]
