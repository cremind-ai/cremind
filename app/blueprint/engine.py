"""Blueprint export engine — build a ``.cremind-blueprint`` archive.

Public surface (lazy-imports ``app.*`` internals so ``cremind blueprint inspect``
can read a manifest with the server stopped):

- :func:`create_blueprint` — package a profile's selected design into an archive
- :func:`read_blueprint_manifest` — read ``manifest.json`` without a full extract
- :func:`audit_no_secrets` — fail-closed guard run before any bytes are written

Archive layout (tar+gzip): ``manifest.json`` (first), ``components/*.json``
(alphabetical), ``skills/<dir>/**`` (bundled user-skill trees), ``inventory.json``
(last). See :mod:`app.blueprint.manifest`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.blueprint.manifest import (
    ARCHIVE_SUFFIX,
    COMPONENT_KEYS,
    COMPONENTS_PREFIX,
    INVENTORY_MEMBER,
    MANIFEST_MEMBER,
    SKILLS_PREFIX,
    BlueprintError,
    BlueprintManifest,
    ComponentEntry,
    SourcePaths,
    compute_min_app_version,
    now_iso,
)

ProgressFn = Callable[[str, int, int], None]

# Hard cap on total archive size — matches the skill-import cap and keeps a
# blueprint Hub-transportable.
_MAX_BYTES = 100 * 1024 * 1024

# Names that must never appear as a bundled skill member (fail-closed audit).
import re as _re

# A doc key whose name looks like it holds a secret value (matched on the key
# name, not the value). Includes ``api_key``/``access_key`` and a bare ``key``.
_CREDENTIAL_NAME_RE = _re.compile(
    r"(?i)(secret|password|passwd|credential|token|api[_-]?key|access[_-]?key|\bkey\b)"
)

# Whitelisted document keys that legitimately hold *names* of secrets (never
# values), so the audit doesn't false-positive on them.
_SECRET_NAME_ARRAYS = frozenset({"secret_variables", "required_secrets"})

# User-controlled config subtrees whose *keys* are arbitrary (a tool argument or
# variable may legitimately be named "key" / "password" without being a secret —
# the is_secret flag, applied in components.py, is the authoritative filter for
# those). The audit skips flagging inside these so it never blocks a legitimate
# export; it still descends structurally.
_USER_KEY_CONTAINERS = frozenset({"config", "variables", "arg", "llm", "meta", "settings"})


@dataclass
class ExportOptions:
    profile: str
    name: str = "blueprint"
    display_name: str = ""
    description: str = ""
    author: str | None = None
    components: set[str] = field(default_factory=lambda: set(COMPONENT_KEYS))
    skill_slugs: set[str] | None = None  # None => all detected skills
    tool_ids: set[str] | None = None  # None => all configured tools
    setting_keys: set[str] | None = None  # None => all changed settings
    event_ids: set[str] | None = None  # None => all events


@dataclass
class ExportResult:
    path: Path
    bytes_written: int
    manifest: BlueprintManifest
    warnings: list[str] = field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────────────────────


def _system_dir() -> str:
    from app.config.settings import BaseConfig

    return BaseConfig.CREMIND_SYSTEM_DIR


def _source_paths() -> SourcePaths:
    from app.config.settings import get_user_working_directory

    try:
        uwd = get_user_working_directory()
    except Exception:  # noqa: BLE001
        uwd = ""
    return SourcePaths(
        system_dir=_system_dir(),
        home_dir=os.path.expanduser("~"),
        user_working_dir=uwd or "",
        sep=os.sep,
        case_insensitive=(sys.platform == "win32"),
    )


def _add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes) -> str:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    tf.addfile(info, io.BytesIO(data))
    return hashlib.sha256(data).hexdigest()


def _component_summary(key: str, doc: dict) -> dict[str, Any]:
    """Per-component manifest summary (counts/names) for the UI + Hub."""
    data = doc.get("data") or {}
    if key == "tools":
        return {"count": len(data.get("tools") or [])}
    if key == "settings":
        return {"count": len(data.get("values") or {})}
    if key == "skills":
        skills = data.get("skills") or []
        return {
            "count": len(skills),
            "names": [s.get("name") for s in skills],
            "approx_bytes": sum(int(s.get("approx_bytes") or 0) for s in skills),
        }
    if key == "events":
        return {
            "counts": {
                "schedule": len(data.get("schedule") or []),
                "file_watcher": len(data.get("file_watcher") or []),
                "skill_event": len(data.get("skill_event") or []),
            }
        }
    if key == "listeners":
        return {"count": len(data.get("listeners") or [])}
    return {}


# ── secret audit (fail-closed) ─────────────────────────────────────────────────


def _walk_doc_for_secrets(node: Any, path: str, offenders: list[str], *, flag: bool = True) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SECRET_NAME_ARRAYS:
                continue  # arrays of secret *names* — allowed
            child_flag = flag and key not in _USER_KEY_CONTAINERS
            if child_flag and isinstance(value, str) and value and _CREDENTIAL_NAME_RE.search(key):
                offenders.append(f"{path}.{key}")
            _walk_doc_for_secrets(value, f"{path}.{key}", offenders, flag=child_flag)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_doc_for_secrets(item, f"{path}[{i}]", offenders, flag=flag)


def audit_no_secrets(planned_skill_members: list[str], docs: dict[str, dict]) -> None:
    """Refuse the export if anything looks like it carries a secret.

    (a) A bundled skill member whose basename looks like a credential store
        (dotfile or ``*token*``/``*secret*``/``*credential*``/``*password*``).
    (b) A component-doc key whose name looks secret and holds a non-empty string
        value (secret *name* arrays are whitelisted).
    Raises :class:`BlueprintError` on the first offense — export never leaks.
    """
    from app.blueprint.rules import is_credential_member

    for member in planned_skill_members:
        base = os.path.basename(member)
        if is_credential_member(base):
            raise BlueprintError(
                f"Refusing to export: bundled skill file {member!r} looks like a "
                f"credential store. This is a bug in the skill file filter."
            )

    offenders: list[str] = []
    for key, doc in docs.items():
        _walk_doc_for_secrets(doc.get("data") or {}, key, offenders)
    if offenders:
        raise BlueprintError(
            "Refusing to export: these fields look like they carry secret values: "
            + ", ".join(offenders)
        )


# ── create ─────────────────────────────────────────────────────────────────


def _validate_skill_dependencies(docs: dict[str, dict]) -> None:
    """Raise if a skill_event / listener references a *user* skill not bundled.

    Built-in skills always exist in the target's fresh profile, so a reference to
    one is always satisfiable; only non-built-in (bundled) skills must be present
    in the skills component.
    """
    skills = (docs.get("skills") or {}).get("data", {}).get("skills") or []
    included_user_slugs = {s["slug"] for s in skills if s.get("bundled")}
    included_user_dirs = {s["dir"] for s in skills if s.get("bundled")}
    # A user skill is one that is bundled; a referenced skill not among the
    # built-ins-or-bundled set is a dependency error only when it is a user skill.
    bundled_slugs = {s["slug"] for s in skills}
    bundled_dirs = {s["dir"] for s in skills}

    missing: list[str] = []

    events = (docs.get("events") or {}).get("data", {})
    for row in events.get("skill_event") or []:
        slug = row.get("skill_slug")
        if slug and slug not in bundled_slugs:
            # Could still be a built-in present on the target; only flag if the
            # skills component is present but omits it (best-effort signal).
            if "skills" in docs and slug not in included_user_slugs:
                missing.append(f"skill_event → skill {slug!r}")

    listeners = (docs.get("listeners") or {}).get("data", {})
    for li in listeners.get("listeners") or []:
        d = li.get("skill_dir")
        if d and d not in bundled_dirs and "skills" in docs and d not in included_user_dirs:
            missing.append(f"listener → skill dir {d!r}")

    # Only a hard failure when the skills component is entirely absent yet
    # non-built-in references exist. Built-in references are always fine.
    if missing and "skills" not in docs:
        raise BlueprintError(
            "This blueprint references skills that are not included. Add the "
            "skills component (or the specific skills): " + "; ".join(missing)
        )


def create_blueprint(
    options: ExportOptions, progress: ProgressFn | None = None
) -> ExportResult:
    from app.__version__ import __version__ as ver
    from app.blueprint.components import BUILDERS, _secret_map, collect_skill_entries
    from app.blueprint.store import blueprints_root, slug_filename

    def _p(phase: str, cur: int = 0, total: int = 0) -> None:
        if progress is not None:
            try:
                progress(phase, cur, total)
            except Exception:  # noqa: BLE001
                pass

    profile = options.profile
    selected = {c for c in options.components if c in COMPONENT_KEYS}
    if not selected:
        raise BlueprintError("No components selected for export.")

    _p("building")
    secret_map = _secret_map(profile)

    # Build each selected component doc + collect requirements.
    docs: dict[str, dict] = {}
    requirements: list[dict] = []
    warnings: list[str] = []

    for key in COMPONENT_KEYS:
        if key not in selected:
            continue
        builder = BUILDERS[key]
        if key == "tools":
            doc, reqs = builder(
                profile, selected_tool_ids=options.tool_ids, secret_map=secret_map
            )
        elif key == "skills":
            doc, reqs = builder(
                profile, selected_slugs=options.skill_slugs, secret_map=secret_map
            )
        elif key == "settings":
            doc, reqs = builder(profile, selected_keys=options.setting_keys)
        elif key == "events":
            doc, reqs = builder(profile, selected_event_ids=options.event_ids)
        else:
            doc, reqs = builder(profile)
        # Surface any builder warnings (e.g. scrubbed base_urls) and drop the
        # private ``_warnings`` key from the persisted doc.
        data = doc.get("data") or {}
        warnings.extend(data.pop("_warnings", []) or [])
        docs[key] = doc
        requirements.extend(reqs)

    _validate_skill_dependencies(docs)

    # Determine bundled skill file members (non-built-in skills in the doc).
    planned_members: list[tuple[str, str]] = []  # (abs_path, arcname)
    bundled_names: list[str] = []
    skills_doc = docs.get("skills")
    if skills_doc:
        from app.blueprint.rules import iter_skill_files

        entries = {e["slug"]: e for e in collect_skill_entries(profile, secret_map)}
        for s in skills_doc["data"]["skills"]:
            if not s.get("bundled"):
                continue
            entry = entries.get(s["slug"])
            if entry is None:
                continue
            skill_abs = str(entry_dir(profile, entry["dir"]))
            for abs_path, rel in iter_skill_files(skill_abs):
                arc = f"{SKILLS_PREFIX}{s['dir']}/{rel}"
                planned_members.append((abs_path, arc))
            bundled_names.append(s["dir"])

    # Fail-closed audit before any bytes are written.
    audit_no_secrets([m[1] for m in planned_members], docs)

    # Assemble the manifest.
    component_entries: dict[str, ComponentEntry] = {}
    for key, doc in docs.items():
        member = f"{COMPONENTS_PREFIX}{key}.json"
        component_entries[key] = ComponentEntry(
            version=doc["version"], member=member, summary=_component_summary(key, doc)
        )

    manifest = BlueprintManifest(
        app_version=ver,
        platform=sys.platform,
        source_profile=profile,
        source_paths=_source_paths(),
        name=slug_filename(options.name or options.display_name or "blueprint"),
        display_name=options.display_name or options.name or profile,
        description=options.description or "",
        author=options.author,
        components=component_entries,
        requirements=_group_requirements(requirements),
        encrypted=False,
        created_at=now_iso(),
    )
    manifest.min_app_version = compute_min_app_version(component_entries)

    # Write the archive.
    _p("archiving")
    root = blueprints_root()
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    dest = root / f"{manifest.name}-{ts}{ARCHIVE_SUFFIX}"
    dest_part = dest.with_suffix(dest.suffix + ".part")

    inventory: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    with tarfile.open(str(dest_part), mode="w:gz") as tf:
        # manifest.json (first)
        man_bytes = json.dumps(manifest.to_dict(), indent=2).encode("utf-8")
        inventory[MANIFEST_MEMBER] = {
            "sha256": _add_bytes(tf, MANIFEST_MEMBER, man_bytes),
            "bytes": len(man_bytes),
        }

        # components/*.json (alphabetical, deterministic)
        for key in sorted(docs):
            member = f"{COMPONENTS_PREFIX}{key}.json"
            body = json.dumps(docs[key], indent=2).encode("utf-8")
            inventory[member] = {"sha256": _add_bytes(tf, member, body), "bytes": len(body)}

        # skills/<dir>/** (deterministic order)
        for abs_path, arc in sorted(planned_members, key=lambda m: m[1]):
            try:
                data = Path(abs_path).read_bytes()
            except OSError as e:
                warnings.append(f"skipped skill file {arc}: {e}")
                continue
            total_bytes += len(data)
            if total_bytes > _MAX_BYTES:
                raise BlueprintError(
                    f"Blueprint exceeds the {_MAX_BYTES // (1024 * 1024)} MiB size cap."
                )
            inventory[arc] = {"sha256": _add_bytes(tf, arc, data), "bytes": len(data)}

        # inventory.json (last)
        inv_bytes = json.dumps({"members": inventory}, indent=2).encode("utf-8")
        _add_bytes(tf, INVENTORY_MEMBER, inv_bytes)

    os.replace(dest_part, dest)
    bytes_written = dest.stat().st_size if dest.exists() else 0
    _p("done")

    from app.utils.logger import logger

    logger.info(
        f"[blueprint] created {dest.name} components={sorted(docs)} "
        f"skills={bundled_names} bytes={bytes_written}"
    )
    return ExportResult(
        path=dest, bytes_written=bytes_written, manifest=manifest, warnings=warnings
    )


def entry_dir(profile: str, dir_name: str) -> Path:
    from app.skills.sync import profile_skills_dir

    return profile_skills_dir(profile) / dir_name


def _group_requirements(reqs: list[dict]) -> dict[str, Any]:
    """Group flat requirement descriptors into the manifest's shape."""
    secrets: list[dict] = []
    paths: list[dict] = []
    listeners: list[dict] = []
    for r in reqs:
        comp = r.get("component")
        if comp == "events" and r.get("kind") == "file_watcher":
            paths.append(r)
        elif comp == "listeners":
            listeners.append(r)
        else:
            secrets.append(r)
    return {"secrets": secrets, "paths": paths, "listeners": listeners}


# ── read ─────────────────────────────────────────────────────────────────────


def read_blueprint_manifest(archive: Path) -> BlueprintManifest:
    """Read ``manifest.json`` from an archive without extracting everything."""
    archive = Path(archive)
    with tarfile.open(str(archive), mode="r:gz") as tf:
        for member in tf:
            if member.name == MANIFEST_MEMBER:
                fh = tf.extractfile(member)
                if fh is None:
                    break
                return BlueprintManifest.from_dict(json.loads(fh.read().decode("utf-8")))
    raise BlueprintError("Blueprint archive has no manifest.json.")


__all__ = [
    "ExportOptions",
    "ExportResult",
    "audit_no_secrets",
    "create_blueprint",
    "read_blueprint_manifest",
]
