"""Import staging + plan computation.

Upload → traversal-safe extract → :func:`app.blueprint.manifest.assert_importable`
→ a per-step *plan* the wizard renders directly. The plan lists, for each
importable component, the step's requirement descriptors computed from the
manifest and local state: secrets to enter, LLM SDKs that may be missing,
file-watcher paths that need confirming, listeners to register, and notify-only
previews (settings that will apply, events that will run).
"""

from __future__ import annotations

import json
import os
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

from app.blueprint.manifest import (
    COMPONENT_KEYS,
    COMPONENTS_PREFIX,
    MANIFEST_MEMBER,
    BlueprintError,
    BlueprintManifest,
    assert_importable,
)
from app.blueprint.session import STATE_STAGED, ImportSession
from app.blueprint.store import session_dir, sweep_stale_sessions
from app.utils.logger import logger

_MAX_BYTES = 100 * 1024 * 1024

# The canonical wizard order (applied to the current profile, in order).
_STEP_ORDER = ("settings", "persona", "llm", "tools", "skills", "events", "listeners")

_STEP_TITLES = {
    "settings": "Settings",
    "persona": "Agent persona",
    "llm": "LLM provider",
    "tools": "Tools",
    "skills": "Skills",
    "events": "Events",
    "listeners": "Listeners",
}


# ── staging ─────────────────────────────────────────────────────────────────


def _safe_extract(archive_path: Path, dest: Path) -> None:
    """Extract a ``.cremind-blueprint`` archive into ``dest``, safely.

    Rejects absolute/``..`` member paths, symlinks/hardlinks, a total extracted
    size over the cap, and skill-dir names that collide only by case (which
    would clobber on a case-insensitive filesystem).
    """
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    seen_casefold: dict[str, str] = {}
    with tarfile.open(str(archive_path), mode="r:gz") as tf:
        for member in tf:
            name = member.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise BlueprintError(f"Unsafe archive member path: {member.name!r}")
            if member.issym() or member.islnk():
                raise BlueprintError(f"Archive contains a link member: {member.name!r}")
            fold = name.casefold()
            if fold in seen_casefold and seen_casefold[fold] != name:
                raise BlueprintError(
                    f"Archive has case-colliding members ({seen_casefold[fold]!r} vs "
                    f"{member.name!r}); refusing to extract."
                )
            seen_casefold[fold] = name
            if member.isreg():
                total += member.size
                if total > _MAX_BYTES:
                    raise BlueprintError(
                        f"Blueprint exceeds the {_MAX_BYTES // (1024 * 1024)} MiB size cap."
                    )
            tf.extract(member, str(dest))


def stage_upload(archive_bytes_path: Path, *, owner: str) -> ImportSession:
    """Stage an uploaded blueprint into a fresh session and compute its plan.

    Raises :class:`BlueprintError` / ``BlueprintIncompatibleError`` on a bad or
    incompatible archive (the caller maps these to 4xx).
    """
    sweep_stale_sessions()

    session_id = uuid.uuid4().hex[:16]
    sdir = session_dir(session_id)
    sdir.mkdir(parents=True, exist_ok=True)

    # Copy the raw upload, then extract into payload/.
    import shutil

    saved = sdir / "upload.blueprint"
    shutil.copyfile(archive_bytes_path, saved)

    payload = sdir / "payload"
    _safe_extract(saved, payload)

    man_path = payload / MANIFEST_MEMBER
    if not man_path.is_file():
        raise BlueprintError("Blueprint is missing manifest.json.")
    manifest = BlueprintManifest.from_dict(json.loads(man_path.read_text(encoding="utf-8")))

    report = assert_importable(manifest)  # raises on fatal

    steps, warnings = build_import_plan(payload, manifest, report.supported_components)

    now = time.time()
    session = ImportSession(
        id=session_id,
        owner=owner,
        created_at=now,
        updated_at=now,
        state=STATE_STAGED,
        manifest=manifest.summary(),
        plan=steps,
        # Import applies to the caller's own (current) profile — the one whose
        # Blueprint page launched the wizard. There is no create-profile step:
        # the user creates a fresh profile first (to avoid changing an existing
        # one), then imports into it.
        target_profile=owner,
        steps=[{"key": s["key"], "status": "pending", "requirements": s.get("requirements", []), "result": {}} for s in steps],
        warnings=[{"kind": "compat", "message": w} for w in report.warnings],
    )
    session.save()
    logger.info(f"[blueprint] staged import session {session_id} steps={[s['key'] for s in steps]}")
    return session


# ── plan ─────────────────────────────────────────────────────────────────────


def load_component(payload_dir: Path, key: str) -> dict | None:
    path = Path(payload_dir) / f"{COMPONENTS_PREFIX}{key}.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc.get("data") if isinstance(doc, dict) else None


def build_import_plan(
    payload_dir: Path, manifest: BlueprintManifest, supported: list[str]
) -> tuple[list[dict], list[str]]:
    """Return ``(steps, warnings)`` for the wizard.

    Only components present in the manifest AND supported by this build get a
    step; ``profile`` is always the first step.
    """
    warnings: list[str] = []
    present = set(manifest.components.keys()) & set(supported)
    steps: list[dict] = []

    # No create-profile step: the blueprint applies to the current profile.
    for key in _STEP_ORDER:
        if key not in present:
            continue
        data = load_component(payload_dir, key) or {}
        builder = _PLAN_BUILDERS.get(key)
        step = builder(data, manifest, warnings) if builder else {"requirements": []}
        step.setdefault("key", key)
        step.setdefault("title", _STEP_TITLES.get(key, key))
        step.setdefault("kind", "apply")
        steps.append(step)

    return steps, warnings


def _plan_settings(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    from app.config.user_config import resolve_default

    values = data.get("values") or {}
    exported_defaults = data.get("defaults_at_export") or {}
    rows = []
    for key, value in values.items():
        try:
            target_default = resolve_default(key)
            supported = True
        except KeyError:
            target_default = None
            supported = False
            warnings.append(f"Setting {key!r} is not recognised by this build and will be skipped.")
        rows.append(
            {
                "key": key,
                "blueprint_value": value,
                "exported_default": exported_defaults.get(key),
                "target_default": target_default,
                "supported": supported,
            }
        )
    return {"kind": "notify", "requirements": [], "preview": {"settings": rows}}


def _plan_persona(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    persona = data.get("persona_markdown") or ""
    return {
        "kind": "notify",
        "requirements": [],
        "preview": {
            "agent_name": data.get("agent_name"),
            "persona_chars": len(persona),
            "persona_excerpt": persona[:600],
        },
    }


def _plan_llm(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    from app.features.manifest import LLM_PROVIDER_TO_FEATURE, is_installed

    reqs: list[dict] = []
    for provider in data.get("providers") or []:
        name = provider.get("name")
        req_secrets = provider.get("required_secrets") or []
        sdk_feature = None
        sdk_missing = False
        if name and not name.startswith("custom:"):
            sdk_feature = LLM_PROVIDER_TO_FEATURE.get(name, "llm.openai_compatible")
        elif name:
            sdk_feature = "llm.openai_compatible"
        if sdk_feature:
            try:
                sdk_missing = not is_installed(sdk_feature)
            except KeyError:
                sdk_missing = False
        if req_secrets or sdk_missing:
            reqs.append(
                {
                    "type": "llm_provider",
                    "provider": name,
                    "fields": req_secrets,
                    "sdk_feature": sdk_feature,
                    "sdk_missing": sdk_missing,
                }
            )
    return {
        "requirements": reqs,
        "preview": {"default_provider": data.get("default_provider"), "model_groups": data.get("model_groups")},
    }


def _plan_tools(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    from app.storage.tool_storage import get_tool_storage

    ts = get_tool_storage()
    reqs: list[dict] = []
    preview: list[dict] = []
    for tool in data.get("tools") or []:
        tool_id = tool.get("tool_id")
        secrets = tool.get("secret_variables") or []
        if secrets:
            reqs.append({"type": "tool_secrets", "tool_id": tool_id, "variables": secrets})

        # Friendly name: a2a/mcp definition, else the tools-table row (built-ins
        # exist on the importing machine), else the raw id.
        defn = tool.get("definition") or {}
        row = ts.get_tool(tool_id) or {}
        name = defn.get("name") or row.get("name") or tool_id

        # Flatten the non-secret config into {key: value} for display (secret
        # values are never in the doc; their names live in secret_variables).
        cfg = tool.get("config") or {}
        settings: dict = {}
        for scope in ("arg", "llm", "meta"):
            settings.update(cfg.get(scope) or {})
        settings.update(tool.get("variables") or {})

        preview.append(
            {
                "tool_id": tool_id,
                "name": name,
                "kind": tool.get("kind"),
                "settings": settings,
                "secret_variables": secrets,
                "disabled_leaves": len(tool.get("disabled_leaves") or []),
            }
        )
    return {"requirements": reqs, "preview": {"tools": preview}}


def _plan_skills(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    from app.skills.sync import builtin_skill_dir_names

    builtins = builtin_skill_dir_names()
    reqs: list[dict] = []
    for skill in data.get("skills") or []:
        dir_name = skill.get("dir")
        env_secrets = sorted(
            {v["name"] for v in skill.get("environment_variables") or [] if v.get("secret")}
            | set(skill.get("secret_variables") or [])
        )
        # Against a fresh profile, the only possible conflict is a built-in name,
        # which is forced keep-local (boot resync would overwrite a blueprint copy).
        conflict = "builtin" if dir_name in builtins else None
        reqs.append(
            {
                "type": "skill",
                "slug": skill.get("slug"),
                "dir": dir_name,
                "name": skill.get("name"),
                "bundled": skill.get("bundled"),
                "conflict": conflict,
                "secret_variables": env_secrets,
            }
        )
    return {"requirements": reqs}


def _plan_events(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    from app.backup.paths import build_path_map, relocate_path

    pm = build_path_map(manifest, _system_dir(), os.path.expanduser("~"))

    watcher_reqs: list[dict] = []
    for w in data.get("file_watcher") or []:
        src = w.get("root_path") or ""
        suggested, changed, _abs = relocate_path(pm, src)
        exists = bool(suggested) and os.path.isdir(suggested)
        watcher_reqs.append(
            {
                "type": "watcher_path",
                "name": w.get("name"),
                "source_root_path": src,
                "suggested_root_path": suggested,
                "relocated": changed,
                "exists": exists,
            }
        )

    schedule_preview = [
        {"title": s.get("title"), "rrule": s.get("rrule"), "dtstart": s.get("dtstart")}
        for s in data.get("schedule") or []
    ]
    skill_event_preview = [
        {"skill_slug": e.get("skill_slug"), "event_type": e.get("event_type")}
        for e in data.get("skill_event") or []
    ]
    return {
        "kind": "notify",
        "requirements": watcher_reqs,
        "preview": {
            "calendar_schedule_enabled": data.get("calendar_schedule_enabled"),
            "schedule": schedule_preview,
            "skill_event": skill_event_preview,
        },
    }


def _plan_listeners(data: dict, manifest: BlueprintManifest, warnings: list[str]) -> dict:
    reqs = [
        {"type": "listener", "skill_dir": li.get("skill_dir")}
        for li in data.get("listeners") or []
    ]
    return {"requirements": reqs}


def _system_dir() -> str:
    from app.config.settings import BaseConfig

    return BaseConfig.CREMIND_SYSTEM_DIR


_PLAN_BUILDERS = {
    "settings": _plan_settings,
    "persona": _plan_persona,
    "llm": _plan_llm,
    "tools": _plan_tools,
    "skills": _plan_skills,
    "events": _plan_events,
    "listeners": _plan_listeners,
}


__all__ = ["build_import_plan", "load_component", "stage_upload"]
