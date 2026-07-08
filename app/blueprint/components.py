"""Export-side builders for each blueprint component document.

Every builder reads a profile's *design* from storage/disk and returns
``(doc, requirements)``:

- ``doc`` is the ``{"component", "version", "data"}`` JSON written to
  ``components/<key>.json`` — carrying only non-secret design, never a secret
  value.
- ``requirements`` is a list of descriptors the manifest aggregates so the
  import wizard (and a future Hub listing) can prompt for what the target must
  supply: secret keys to enter, machine-specific paths to confirm, listeners to
  register.

Secret hygiene is enforced here in two ways: values are read with
``include_secrets=False`` (so a secret comes back masked as ``"***"``), and a
per-key ``is_secret`` map (from the existence-only storage listings) is used to
drop secret keys outright and surface only their *names*. The engine adds a
final fail-closed audit on top (:func:`app.blueprint.engine.audit_no_secrets`).

All ``app.*`` imports are done inside functions so this module stays importable
without the server up (the offline ``cremind blueprint inspect`` path).
"""

from __future__ import annotations

from typing import Any

from app.blueprint.manifest import SUPPORTED_COMPONENT_VERSIONS

_MASK = "***"


# ── storage accessors (lazy) ─────────────────────────────────────────────────


def _tool_storage():
    from app.storage.tool_storage import get_tool_storage

    return get_tool_storage()


def _config_storage():
    from app.storage.dynamic_config_storage import DynamicConfigStorage

    return DynamicConfigStorage()


def _config_mgr():
    from app.tools.config_manager import ToolConfigManager

    return ToolConfigManager(_tool_storage())


def _doc(component: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "component": component,
        "version": SUPPORTED_COMPONENT_VERSIONS[component],
        "data": data,
    }


def _secret_map(profile: str) -> dict[str, dict[str, set[str]]]:
    """Return ``{tool_id: {scope: {secret_key, ...}}}`` for a profile.

    Existence-only — reads which keys are secrets, never their values.
    """
    out: dict[str, dict[str, set[str]]] = {}
    for row in _tool_storage().list_config_keys(profile):
        if row["is_secret"]:
            out.setdefault(row["tool_id"], {}).setdefault(row["scope"], set()).add(row["key"])
    return out


def _scrub(values: dict[str, Any], secret_keys: set[str]) -> dict[str, Any]:
    """Drop secret-flagged keys and any masked value (belt and braces)."""
    return {
        k: v
        for k, v in values.items()
        if k not in secret_keys and v != _MASK
    }


def _tool_config_bundle(
    tool_id: str,
    profile: str,
    secret_map: dict[str, dict[str, set[str]]],
    *,
    exclude_meta_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble the exportable config for one tool_id (non-skill or skill).

    Returns ``{config: {arg, llm, meta}, variables, secret_variables,
    disabled_leaves}`` with all secret values stripped and secret variable
    *names* preserved in ``secret_variables``.
    """
    mgr = _config_mgr()
    tool_secrets = secret_map.get(tool_id, {})

    args = _scrub(mgr.get_arguments(tool_id, profile), tool_secrets.get("arg", set()))
    llm = _scrub(mgr.get_llm_params(tool_id, profile), tool_secrets.get("llm", set()))
    meta = _scrub(dict(mgr.get_meta(tool_id, profile)), tool_secrets.get("meta", set()))
    for key in exclude_meta_keys:
        meta.pop(key, None)

    var_secret_keys = tool_secrets.get("variable", set())
    variables = _scrub(
        mgr.get_variables(tool_id, profile, include_secrets=False), var_secret_keys
    )
    secret_variables = sorted(var_secret_keys)

    disabled_leaves = sorted(mgr.get_disabled_leaves(tool_id, profile))

    return {
        "config": {"arg": args, "llm": llm, "meta": meta},
        "variables": variables,
        "secret_variables": secret_variables,
        "disabled_leaves": disabled_leaves,
    }


def _is_empty_config(bundle: dict[str, Any]) -> bool:
    cfg = bundle["config"]
    return (
        not cfg["arg"]
        and not cfg["llm"]
        and not cfg["meta"]
        and not bundle["variables"]
        and not bundle["secret_variables"]
        and not bundle["disabled_leaves"]
    )


# ── persona ──────────────────────────────────────────────────────────────────


def build_persona_doc(profile: str) -> tuple[dict, list]:
    from app.utils.agent_name import read_agent_name
    from app.utils.persona import read_persona_file

    data = {
        "persona_markdown": read_persona_file(profile),
        "agent_name": read_agent_name(profile),
    }
    return _doc("persona", data), []


# ── tools (builtin + a2a/mcp; skills live in the skills component) ─────────────


def _profile_prefix(profile: str) -> str:
    return f"{profile}__"


def _configured_tool_ids(profile: str) -> set[str]:
    """tool_ids the profile has customized — from profile_tools + tool_configs."""
    ts = _tool_storage()
    ids: set[str] = set(ts.list_profile_tools(profile).keys())
    for row in ts.list_config_keys(profile):
        ids.add(row["tool_id"])
    return ids


def build_tools_doc(profile: str, *, secret_map: dict | None = None) -> tuple[dict, list]:
    from app.calendar.feature import FEATURE_KEY, SCHEDULER_TOOL_ID

    ts = _tool_storage()
    secret_map = secret_map if secret_map is not None else _secret_map(profile)
    prefix = _profile_prefix(profile)
    enabled_map = ts.list_profile_tools(profile)

    tools: list[dict] = []
    requirements: list[dict] = []

    for tool_id in sorted(_configured_tool_ids(profile)):
        if tool_id.startswith(prefix):
            continue  # a skill tool — handled by the skills component
        row = ts.get_tool(tool_id)
        kind = (row or {}).get("tool_type")
        if kind == "skill":
            continue  # defensive: skill classified by type

        exclude_meta = (FEATURE_KEY,) if tool_id == SCHEDULER_TOOL_ID else ()
        bundle = _tool_config_bundle(
            tool_id, profile, secret_map, exclude_meta_keys=exclude_meta
        )

        entry: dict[str, Any] = {"tool_id": tool_id, "kind": kind or "builtin"}

        if kind in ("a2a", "mcp"):
            entry["enabled"] = enabled_map.get(tool_id)
            if row is not None:
                entry["definition"] = {
                    "name": row.get("name"),
                    "type": kind,
                    "source": row.get("source"),
                    "description": row.get("description"),
                    "path_fields": ["source"],
                }
        else:
            entry["enabled"] = None  # built-ins are gated per-leaf, not by a flag

        entry.update(bundle)

        # Skip a built-in with no customization at all (nothing to carry).
        if entry.get("enabled") is None and _is_empty_config(bundle):
            continue

        tools.append(entry)
        for var in bundle["secret_variables"]:
            requirements.append(
                {"component": "tools", "tool_id": tool_id, "variable": var}
            )

    return _doc("tools", {"tools": tools}), requirements


# ── llm ────────────────────────────────────────────────────────────────────


def _split_provider_key(key: str) -> tuple[str | None, str | None]:
    """Split ``<provider>.<field>`` respecting the ``custom:<slug>`` prefix.

    Returns ``(provider, field)`` or ``(None, None)`` for non-provider keys
    (``model_group.*``, ``default_provider``, ``custom_providers``).
    """
    if key in ("default_provider", "custom_providers") or key.startswith("model_group"):
        return None, None
    if key.startswith("custom:"):
        rest = key[len("custom:"):]
        slug, _, field = rest.partition(".")
        if not field:
            return None, None
        return f"custom:{slug}", field
    provider, _, field = key.partition(".")
    if not field:
        return None, None
    return provider, field


def _scrub_base_url(base_url: str) -> tuple[str, bool]:
    """Strip embedded credentials from a custom provider base_url.

    Removes userinfo (``user:pass@``) and any query param whose name looks like
    a secret. Returns ``(cleaned_url, was_scrubbed)``.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if not base_url:
        return base_url, False
    try:
        parts = urlsplit(base_url)
    except Exception:  # noqa: BLE001
        return base_url, False

    scrubbed = False
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        scrubbed = True

    kept_q = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if any(tok in name.lower() for tok in ("secret", "key", "password", "token")):
            scrubbed = True
            continue
        kept_q.append((name, value))
    query = urlencode(kept_q)

    if not scrubbed:
        return base_url, False
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)), True


def build_llm_doc(profile: str) -> tuple[dict, list]:
    import json

    cs = _config_storage()
    # Existence-only secret map for llm_config keys.
    secret_keys = {r["key"] for r in cs.list_keys("llm_config", profile) if r["is_secret"]}
    all_vals = cs.get_all("llm_config", include_secrets=False, profile=profile)

    default_provider = all_vals.get("default_provider")

    # Group non-secret provider fields and secret field names by provider.
    provider_settings: dict[str, dict[str, str]] = {}
    provider_secret_fields: dict[str, list[str]] = {}
    for key, value in all_vals.items():
        provider, field = _split_provider_key(key)
        if provider is None:
            continue
        if key in secret_keys or value == _MASK:
            provider_secret_fields.setdefault(provider, []).append(field)
        else:
            provider_settings.setdefault(provider, {})[field] = value
    # A secret-only provider (e.g. only ``<p>.api_key`` set) still needs an entry.
    for key in secret_keys:
        provider, field = _split_provider_key(key)
        if provider is not None:
            provider_secret_fields.setdefault(provider, [])
            if field and field not in provider_secret_fields[provider]:
                provider_secret_fields[provider].append(field)

    # custom_providers registry (non-secret JSON row) — scrub base_urls.
    custom_providers: dict[str, Any] = {}
    raw_custom = all_vals.get("custom_providers")
    warnings: list[str] = []
    if raw_custom and raw_custom != _MASK:
        try:
            parsed = json.loads(raw_custom) if isinstance(raw_custom, str) else raw_custom
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict):
            for slug, entry in parsed.items():
                if not isinstance(entry, dict):
                    continue
                cleaned = dict(entry)
                base_url = cleaned.get("base_url") or ""
                new_url, did = _scrub_base_url(base_url)
                if did:
                    cleaned["base_url"] = new_url
                    warnings.append(
                        f"Stripped embedded credentials from custom provider "
                        f"{slug!r} base_url."
                    )
                custom_providers[slug] = cleaned

    all_provider_names = set(provider_settings) | set(provider_secret_fields)
    if default_provider:
        all_provider_names.add(default_provider)

    providers: list[dict] = []
    requirements: list[dict] = []
    for name in sorted(all_provider_names):
        builtin = not name.startswith("custom:")
        settings = provider_settings.get(name, {})
        req_secrets = sorted(set(provider_secret_fields.get(name, [])))
        providers.append(
            {
                "name": name,
                "builtin": builtin,
                "auth_method": settings.get("auth_method"),
                "required_secrets": req_secrets,
                "settings": settings,
            }
        )
        for field in req_secrets:
            requirements.append(
                {"component": "llm", "provider": name, "field": field}
            )

    # model groups
    def _mg(group: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        model = all_vals.get(f"model_group.{group}")
        if model and model != _MASK:
            out["model"] = model
        eff = all_vals.get(f"model_group.{group}.reasoning_effort")
        if eff and eff != _MASK:
            out["reasoning_effort"] = eff
        if group == "vision":
            en = all_vals.get("model_group.vision.enabled")
            if en is not None and en != _MASK:
                out["enabled"] = str(en).strip().lower() in ("1", "true", "yes", "on")
        return out

    model_groups = {g: _mg(g) for g in ("high", "vision", "low")}
    model_groups = {g: v for g, v in model_groups.items() if v}

    data = {
        "default_provider": default_provider,
        "providers": providers,
        "custom_providers": custom_providers,
        "model_groups": model_groups,
        "_warnings": warnings,
    }
    return _doc("llm", data), requirements


# ── settings (changed-from-default user_config rows) ───────────────────────────


def build_settings_doc(profile: str) -> tuple[dict, list]:
    from app.config.user_config import resolve_default

    values = _config_storage().get_all("user_config", profile=profile)
    defaults_at_export: dict[str, Any] = {}
    for key in values:
        try:
            defaults_at_export[key] = resolve_default(key)
        except KeyError:
            continue  # not a schema key on this build — carried but unlabelled
    data = {"values": values, "defaults_at_export": defaults_at_export}
    return _doc("settings", data), []


# ── skills ─────────────────────────────────────────────────────────────────


def _skill_env_var_specs(metadata: dict) -> list[dict]:
    env = metadata.get("environment_variables") if isinstance(metadata, dict) else None
    if not isinstance(env, list):
        return []
    specs: list[dict] = []
    for item in env:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        specs.append(
            {
                "name": item.get("name"),
                "description": item.get("description", ""),
                "required": bool(item.get("required", False)),
                "secret": bool(item.get("secret", False)),
                "type": item.get("type", "string"),
                "default": item.get("default"),
            }
        )
    return specs


def collect_skill_entries(profile: str, secret_map: dict | None = None) -> list[dict]:
    """Return one entry per skill dir in the profile, classified + configured.

    Bundled (non-builtin) skills carry a ``files_member_prefix`` and
    ``approx_bytes``; built-ins are config-only (``bundled: false``).
    """
    from app.blueprint.rules import iter_skill_files
    from app.skills.scanner import scan_skills
    from app.skills.sync import is_builtin_skill_dir, profile_skills_dir
    from app.tools.ids import slugify

    secret_map = secret_map if secret_map is not None else _secret_map(profile)
    skills_dir = profile_skills_dir(profile)
    entries: list[dict] = []

    for info in scan_skills(skills_dir).values():
        dir_name = info.dir_path.name
        slug = slugify(info.name)
        tool_id = f"{profile}__{slug}"
        builtin = is_builtin_skill_dir(dir_name)
        bundle = _tool_config_bundle(tool_id, profile, secret_map)

        approx_bytes = 0
        if not builtin:
            for abs_path, _rel in iter_skill_files(str(info.dir_path)):
                try:
                    import os

                    approx_bytes += os.path.getsize(abs_path)
                except OSError:
                    pass

        entries.append(
            {
                "name": info.name,
                "slug": slug,
                "dir": dir_name,
                "builtin": builtin,
                "bundled": not builtin,
                "description": info.description,
                "environment_variables": _skill_env_var_specs(info.metadata),
                "has_listener": isinstance(info.metadata, dict)
                and isinstance(info.metadata.get("long_running_app"), dict),
                "config": bundle["config"],
                "variables": bundle["variables"],
                "secret_variables": bundle["secret_variables"],
                "disabled_leaves": bundle["disabled_leaves"],
                "files_member_prefix": None if builtin else f"skills/{dir_name}/",
                "approx_bytes": approx_bytes,
                "_configured": not _is_empty_config(bundle),
            }
        )
    return entries


def build_skills_doc(
    profile: str,
    *,
    selected_slugs: set[str] | None = None,
    secret_map: dict | None = None,
) -> tuple[dict, list]:
    entries = collect_skill_entries(profile, secret_map)
    if selected_slugs is not None:
        entries = [e for e in entries if e["slug"] in selected_slugs]

    requirements: list[dict] = []
    out: list[dict] = []
    for e in entries:
        e = {k: v for k, v in e.items() if not k.startswith("_")}
        out.append(e)
        declared_secret = {
            s["name"] for s in e["environment_variables"] if s.get("secret")
        }
        for var in sorted(set(e["secret_variables"]) | declared_secret):
            requirements.append(
                {"component": "skills", "skill": e["slug"], "variable": var}
            )
    return _doc("skills", {"skills": out}), requirements


# ── events (schedule + file-watcher + skill-event) ─────────────────────────────


def build_events_doc(profile: str) -> tuple[dict, list]:
    from app.calendar.feature import is_enabled
    from app.skills.scanner import scan_skills
    from app.skills.sync import profile_skills_dir
    from app.storage.event_subscription_storage import EventSubscriptionStorage
    from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage
    from app.storage.schedule_event_storage import ScheduleEventSubscriptionStorage
    from app.tools.ids import slugify

    requirements: list[dict] = []

    # Schedules — drop external (Google mirror) rows, cancelled rows, and all
    # runtime columns; keep the definition.
    schedule_out: list[dict] = []
    excluded_mirrors = 0
    for row in ScheduleEventSubscriptionStorage().list_by_profile(profile):
        if row.get("external_provider"):
            excluded_mirrors += 1
            continue
        if row.get("status") == "cancelled":
            continue
        schedule_out.append(
            {
                "title": row["title"],
                "action": row["action"],
                "all_day": row["all_day"],
                "schedule_kind": row["schedule_kind"],
                "dtstart": row["dtstart"],
                "duration_minutes": row["duration_minutes"],
                "rrule": row["rrule"],
                "recurrence_end_type": row["recurrence_end_type"],
                "recurrence_end_value": row["recurrence_end_value"],
                "timezone": row["timezone"],
                "status": row["status"],
                "source": row["source"],
            }
        )

    # File watchers — carry root_path raw; it is relocated/prompted on import.
    watcher_out: list[dict] = []
    for row in FileWatcherSubscriptionStorage().list_by_profile(profile):
        watcher_out.append(
            {
                "name": row["name"],
                "root_path": row["root_path"],
                "recursive": row["recursive"],
                "target_kind": row["target_kind"],
                "event_types": row["event_types"],
                "extensions": row["extensions"],
                "action": row["action"],
                "path_fields": ["root_path"],
            }
        )
        requirements.append(
            {
                "component": "events",
                "kind": "file_watcher",
                "name": row["name"],
                "source_root_path": row["root_path"],
            }
        )

    # Skill events — reference the skill by slug so the id is profile-agnostic.
    name_to_slug = {
        info.name: slugify(info.name)
        for info in scan_skills(profile_skills_dir(profile)).values()
    }
    skill_event_out: list[dict] = []
    for row in EventSubscriptionStorage().list_by_profile(profile):
        skill_event_out.append(
            {
                "skill_slug": name_to_slug.get(row["skill_name"], slugify(row["skill_name"])),
                "skill_name": row["skill_name"],
                "event_type": row["event_type"],
                "action": row["action"],
            }
        )

    data = {
        "calendar_schedule_enabled": is_enabled(profile),
        "schedule": schedule_out,
        "file_watcher": watcher_out,
        "skill_event": skill_event_out,
        "_excluded": {"external_mirrors": excluded_mirrors},
    }
    return _doc("events", data), requirements


# ── listeners (canonical skill listeners only) ─────────────────────────────────


def _canonical_listener_skill_dir(profile: str, working_dir: str, command: str) -> str | None:
    """Return the skill dir name if this autostart row is a canonical skill
    listener (``<profile skills>/<dir>/scripts`` + ``event_listener.py``), else
    None (arbitrary autostart commands are NOT exported)."""
    import os

    from app.skills.sync import profile_skills_dir

    if "event_listener.py" not in (command or ""):
        return None
    wd = os.path.normpath(working_dir or "")
    if os.path.basename(wd) != "scripts":
        return None
    skill_dir = os.path.dirname(wd)
    skills_root = os.path.normpath(str(profile_skills_dir(profile)))
    if os.path.normpath(os.path.dirname(skill_dir)) != skills_root:
        return None
    return os.path.basename(skill_dir)


def build_listeners_doc(profile: str) -> tuple[dict, list]:
    from app.storage import get_autostart_storage

    listeners: list[dict] = []
    requirements: list[dict] = []
    skipped_non_skill = 0

    for row in get_autostart_storage().list(profile):
        skill_dir = _canonical_listener_skill_dir(
            profile, row.get("working_dir", ""), row.get("command", "")
        )
        if skill_dir is None:
            skipped_non_skill += 1
            continue
        listeners.append({"skill_dir": skill_dir, "is_pty": bool(row.get("is_pty"))})
        requirements.append({"component": "listeners", "skill_dir": skill_dir})

    data = {"listeners": listeners, "_skipped_non_skill": skipped_non_skill}
    return _doc("listeners", data), requirements


# ── dispatch table ─────────────────────────────────────────────────────────


BUILDERS = {
    "persona": build_persona_doc,
    "tools": build_tools_doc,
    "llm": build_llm_doc,
    "settings": build_settings_doc,
    "skills": build_skills_doc,
    "events": build_events_doc,
    "listeners": build_listeners_doc,
}


__all__ = [
    "BUILDERS",
    "build_events_doc",
    "build_listeners_doc",
    "build_llm_doc",
    "build_persona_doc",
    "build_settings_doc",
    "build_skills_doc",
    "build_tools_doc",
    "collect_skill_entries",
]
