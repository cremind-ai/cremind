"""Blueprint manifest — the portable, secret-free description of a blueprint.

The manifest is the first member of every ``.cremind-blueprint`` archive. It
records everything an import on a *different* install needs to decide
compatibility and drive the wizard, without extracting the rest of the archive:

- format identity + version (refuse blueprints newer than this build understands)
- the app version the blueprint was authored at (informational — soft-gated,
  unlike backup which hard-gates on schema)
- per-component versions + members (the per-item forward-compat gate)
- the source environment's absolute roots (reused ``SourcePaths`` from the
  backup manifest) — inputs to path relocation for file-watcher roots
- precomputed ``requirements`` (secrets to enter, paths to confirm, listeners to
  register) so the import wizard — and a future Hub listing — can render "you
  will need …" from the manifest alone.

Nothing here is secret: it names providers, skills, and settings; it never
carries an API key, token, or password value.

Compatibility differs deliberately from backup's ``assert_restorable``: there
is **no Alembic-revision gate and no lower app-version floor**, because a
blueprint applies through the target's current-schema storage APIs rather than
loading a row dump. The real gate is per-component: a component whose version
this build doesn't understand is skipped, and the rest of the blueprint imports.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Reuse the backup manifest's source-path descriptor verbatim so the relocation
# code in ``app.backup.paths`` works on blueprints unmodified.
from app.backup.manifest import SourcePaths

BLUEPRINT_FORMAT = "cremind-blueprint"
BLUEPRINT_FORMAT_VERSION = 1

# The archive file extension — distinctive so listings never confuse it with a
# ``.cremind-backup`` full-system archive.
ARCHIVE_SUFFIX = ".cremind-blueprint"

# Member names inside the archive (POSIX; contractual order: manifest first,
# inventory last, components and skills between).
MANIFEST_MEMBER = "manifest.json"
COMPONENTS_PREFIX = "components/"
SKILLS_PREFIX = "skills/"
INVENTORY_MEMBER = "inventory.json"

# The exportable component keys, in the canonical import-apply order. The import
# wizard renders steps in this order; export writes ``components/<key>.json``.
COMPONENT_KEYS = ("persona", "tools", "llm", "settings", "skills", "events", "listeners")

# The component-document versions this build writes and can read. A blueprint
# component whose version exceeds the value here is skipped on import (the rest
# still applies) — this is the per-item forward-compatibility gate.
SUPPORTED_COMPONENT_VERSIONS: dict[str, int] = {k: 1 for k in COMPONENT_KEYS}

# The minimum app version required to understand each (component, version). Used
# purely to compute ``min_app_version`` in the manifest so an *older* importer
# can print an exact "upgrade to >= X" message even for components it has never
# heard of. Bump alongside a component version whenever a new component or a
# breaking component change ships.
COMPONENT_MIN_APP: dict[tuple[str, int], str] = {
    ("persona", 1): "0.0.8",
    ("tools", 1): "0.0.8",
    ("llm", 1): "0.0.8",
    ("settings", 1): "0.0.8",
    ("skills", 1): "0.0.8",
    ("events", 1): "0.0.8",
    ("listeners", 1): "0.0.8",
}


class BlueprintError(Exception):
    """Base class for blueprint engine errors."""


class BlueprintIncompatibleError(BlueprintError):
    """The blueprint cannot be imported by this build (format/version)."""


@dataclass
class ComponentStatus:
    """Per-component import verdict from :func:`check_importable`."""

    supported: bool
    version: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "version": self.version, "reason": self.reason}


@dataclass
class ImportabilityReport:
    """Result of :func:`check_importable`.

    ``fatal`` is ``None`` when the blueprint can be imported (possibly with a
    reduced set of components); a string when it cannot be imported at all.
    ``components`` maps every component named in the manifest to its verdict,
    and ``warnings`` collects soft issues (e.g. authored by a newer build).
    """

    fatal: str | None
    components: dict[str, ComponentStatus] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.fatal is None

    @property
    def supported_components(self) -> list[str]:
        return [k for k, s in self.components.items() if s.supported]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fatal": self.fatal,
            "components": {k: s.to_dict() for k, s in self.components.items()},
            "warnings": list(self.warnings),
        }


@dataclass
class ComponentEntry:
    """A component's manifest descriptor: version + archive member + summary."""

    version: int
    member: str
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"version": self.version, "member": self.member}
        d.update(self.summary)
        return d

    @classmethod
    def from_dict(cls, key: str, d: dict[str, Any]) -> "ComponentEntry":
        summary = {k: v for k, v in d.items() if k not in ("version", "member")}
        return cls(
            version=int(d.get("version") or 1),
            member=d.get("member") or f"{COMPONENTS_PREFIX}{key}.json",
            summary=summary,
        )


@dataclass
class BlueprintManifest:
    app_version: str
    platform: str
    source_profile: str
    source_paths: SourcePaths
    name: str = "blueprint"
    display_name: str = ""
    description: str = ""
    author: str | None = None
    components: dict[str, ComponentEntry] = field(default_factory=dict)
    requirements: dict[str, Any] = field(default_factory=dict)
    min_app_version: str = ""
    encrypted: bool = False
    created_at: str = ""
    format: str = BLUEPRINT_FORMAT
    format_version: int = BLUEPRINT_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "format_version": self.format_version,
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "author": self.author,
            "created_at": self.created_at,
            "app_version": self.app_version,
            "min_app_version": self.min_app_version,
            "platform": self.platform,
            "source_profile": self.source_profile,
            "source_paths": self.source_paths.to_dict(),
            "encrypted": self.encrypted,
            "components": {k: v.to_dict() for k, v in self.components.items()},
            "requirements": self.requirements,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BlueprintManifest":
        comps_raw = d.get("components") or {}
        components = {
            k: ComponentEntry.from_dict(k, v)
            for k, v in comps_raw.items()
            if isinstance(v, dict)
        }
        return cls(
            app_version=d.get("app_version", ""),
            platform=d.get("platform", ""),
            source_profile=d.get("source_profile", ""),
            source_paths=SourcePaths.from_dict(d.get("source_paths") or {}),
            name=d.get("name", "blueprint"),
            display_name=d.get("display_name", ""),
            description=d.get("description", ""),
            author=d.get("author"),
            components=components,
            requirements=dict(d.get("requirements") or {}),
            min_app_version=d.get("min_app_version", ""),
            encrypted=bool(d.get("encrypted", False)),
            created_at=d.get("created_at", ""),
            format=d.get("format", BLUEPRINT_FORMAT),
            format_version=int(d.get("format_version") or 0),
        )

    def summary(self) -> dict[str, Any]:
        """A compact, UI-friendly subset (no path internals)."""
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "author": self.author,
            "app_version": self.app_version,
            "min_app_version": self.min_app_version,
            "platform": self.platform,
            "source_profile": self.source_profile,
            "created_at": self.created_at,
            "components": {k: v.to_dict() for k, v in self.components.items()},
            "requirements": self.requirements,
        }


def now_iso() -> str:
    """UTC ISO-8601 timestamp. Uses time.gmtime (Date.now-free)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def compute_min_app_version(components: dict[str, ComponentEntry]) -> str:
    """The highest per-component ``min_app_version`` over included components.

    So an old importer can tell the user the exact version to upgrade to even
    for a component (or component version) it doesn't recognise.
    """
    from app.upgrade.manifest import parse

    floors = [
        COMPONENT_MIN_APP.get((key, entry.version))
        for key, entry in components.items()
    ]
    floors = [f for f in floors if f]
    if not floors:
        from app.__version__ import __version__ as CURRENT_VERSION

        return CURRENT_VERSION
    return max(floors, key=parse)


def check_importable(manifest: BlueprintManifest) -> ImportabilityReport:
    """Assess whether — and how much of — a blueprint this build can import.

    Fatal (whole blueprint refused), in order:
      - an unrecognised format string;
      - the ``encrypted`` flag set (v1 reserves it, never emits it);
      - a format version newer than this build understands.

    Soft (blueprint proceeds):
      - authored by a newer app build → a warning (per-component versions are
        the real gate, there is no schema coupling to break);
      - a component whose name is unknown, or whose version exceeds what this
        build supports → that component is marked unsupported (the wizard shows
        it greyed) and the rest imports.

    Deliberately absent: the Alembic-revision gate and the
    ``MIN_SUPPORTED_UPGRADE_FROM`` floor from backup's ``assert_restorable`` —
    a blueprint applies through current-schema storage APIs, so any well-formed
    v1 blueprint from any past build imports.
    """
    if manifest.format != BLUEPRINT_FORMAT:
        return ImportabilityReport(
            fatal=(
                f"Unrecognised blueprint format {manifest.format!r} "
                f"(expected {BLUEPRINT_FORMAT!r})."
            )
        )
    if manifest.encrypted:
        return ImportabilityReport(
            fatal="Encrypted blueprints are not supported by this build."
        )
    if manifest.format_version > BLUEPRINT_FORMAT_VERSION:
        need = manifest.min_app_version or "a newer version"
        return ImportabilityReport(
            fatal=(
                f"This blueprint's format version ({manifest.format_version}) is newer "
                f"than this build supports ({BLUEPRINT_FORMAT_VERSION}). Upgrade Cremind "
                f"to {need}, then import."
            )
        )

    report = ImportabilityReport(fatal=None)

    from app.__version__ import __version__ as CURRENT_VERSION

    try:
        from app.upgrade.manifest import is_newer
    except ImportError:
        is_newer = None

    if is_newer is not None and manifest.app_version and is_newer(
        manifest.app_version, CURRENT_VERSION
    ):
        report.warnings.append(
            f"This blueprint was created by Cremind {manifest.app_version}, newer than "
            f"this build ({CURRENT_VERSION}). Unrecognised parts will be skipped; "
            f"upgrade for the full design."
        )

    for key, entry in manifest.components.items():
        supported_version = SUPPORTED_COMPONENT_VERSIONS.get(key)
        if supported_version is None:
            report.components[key] = ComponentStatus(
                supported=False,
                version=entry.version,
                reason="Unknown component — created by a newer Cremind; skipped.",
            )
            report.warnings.append(f"Component {key!r} is not understood by this build and will be skipped.")
        elif entry.version > supported_version:
            report.components[key] = ComponentStatus(
                supported=False,
                version=entry.version,
                reason=(
                    f"Component version {entry.version} is newer than supported "
                    f"({supported_version}); upgrade Cremind to import it."
                ),
            )
            report.warnings.append(
                f"Component {key!r} (v{entry.version}) is newer than this build supports and will be skipped."
            )
        else:
            report.components[key] = ComponentStatus(supported=True, version=entry.version)

    return report


def assert_importable(manifest: BlueprintManifest) -> ImportabilityReport:
    """Raise :class:`BlueprintIncompatibleError` on a fatal incompatibility.

    Returns the full :class:`ImportabilityReport` on success so the caller can
    surface per-component warnings and the supported subset.
    """
    report = check_importable(manifest)
    if report.fatal:
        raise BlueprintIncompatibleError(report.fatal)
    return report


__all__ = [
    "ARCHIVE_SUFFIX",
    "BLUEPRINT_FORMAT",
    "BLUEPRINT_FORMAT_VERSION",
    "COMPONENTS_PREFIX",
    "COMPONENT_KEYS",
    "COMPONENT_MIN_APP",
    "INVENTORY_MEMBER",
    "MANIFEST_MEMBER",
    "SKILLS_PREFIX",
    "SUPPORTED_COMPONENT_VERSIONS",
    "BlueprintError",
    "BlueprintIncompatibleError",
    "BlueprintManifest",
    "ComponentEntry",
    "ComponentStatus",
    "ImportabilityReport",
    "SourcePaths",
    "assert_importable",
    "check_importable",
    "compute_min_app_version",
    "now_iso",
]
