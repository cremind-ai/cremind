"""Backup manifest — the environment-independent description of an archive.

The manifest is the first member of every ``.cremind-backup`` archive (and, for
encrypted archives, a plaintext copy also lives in the encryption envelope
header so it can be read without the passphrase). It records everything a
restore on a *different* machine needs to relocate paths and decide whether the
archive is even compatible with the running build:

- format identity + version (refuse archives newer than this build understands)
- the app version + Alembic revision the DB dump was taken at (drives the
  "create schema at that revision, then upgrade to head" restore recipe)
- the source environment's absolute roots (``system_dir`` / ``home_dir`` /
  ``user_working_dir``) and path separator — the inputs to path relocation
- the DB provider the dump came from (informational — the target keeps its own)

Nothing here is secret: paths, versions, profile names, and row counts only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

BACKUP_FORMAT = "cremind-backup"
BACKUP_FORMAT_VERSION = 1

# The archive file extension. Distinctive so listings can tell a full-system
# backup apart from a raw ``cremind db backup`` snapshot (``*.sqlite.gz`` /
# ``*.pgsnap.gz``) that shares the ``backups/`` directory.
ARCHIVE_SUFFIX = ".cremind-backup"

# Member names inside the archive (POSIX, contractual order: manifest first,
# inventory last).
MANIFEST_MEMBER = "manifest.json"
DB_MEMBER = "db/dump.jsonl.gz"
FILES_PREFIX = "files/"
INVENTORY_MEMBER = "inventory.json"


class BackupError(Exception):
    """Base class for backup/restore engine errors."""


class BackupIncompatibleError(BackupError):
    """The archive cannot be restored by this build (format/version/revision)."""


class BackupPassphraseError(BackupError):
    """The archive is encrypted and the supplied passphrase is missing/wrong."""


@dataclass
class SourcePaths:
    """The source environment's absolute roots — inputs to path relocation."""

    system_dir: str
    home_dir: str
    user_working_dir: str
    sep: str
    case_insensitive: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_dir": self.system_dir,
            "home_dir": self.home_dir,
            "user_working_dir": self.user_working_dir,
            "sep": self.sep,
            "case_insensitive": self.case_insensitive,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SourcePaths":
        return cls(
            system_dir=d.get("system_dir", ""),
            home_dir=d.get("home_dir", ""),
            user_working_dir=d.get("user_working_dir", ""),
            sep=d.get("sep", "/"),
            case_insensitive=bool(d.get("case_insensitive", False)),
        )


@dataclass
class Manifest:
    app_version: str
    alembic_revision: str | None
    db_provider: str
    platform: str
    hostname: str
    source_paths: SourcePaths
    profiles: list[str] = field(default_factory=list)
    db_row_counts: dict[str, int] = field(default_factory=dict)
    files_approx_bytes: int = 0
    browser_profiles_included: bool = True
    encrypted: bool = False
    created_at: str = ""
    format: str = BACKUP_FORMAT
    format_version: int = BACKUP_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "format_version": self.format_version,
            "created_at": self.created_at,
            "app_version": self.app_version,
            "alembic_revision": self.alembic_revision,
            "db_provider": self.db_provider,
            "platform": self.platform,
            "hostname": self.hostname,
            "source_paths": self.source_paths.to_dict(),
            "profiles": list(self.profiles),
            "db": {"member": DB_MEMBER, "row_counts": self.db_row_counts},
            "files": {
                "member_prefix": FILES_PREFIX,
                "approx_total_bytes": self.files_approx_bytes,
                "browser_profiles_included": self.browser_profiles_included,
            },
            "encrypted": self.encrypted,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        db = d.get("db") or {}
        files = d.get("files") or {}
        return cls(
            app_version=d.get("app_version", ""),
            alembic_revision=d.get("alembic_revision"),
            db_provider=d.get("db_provider", ""),
            platform=d.get("platform", ""),
            hostname=d.get("hostname", ""),
            source_paths=SourcePaths.from_dict(d.get("source_paths") or {}),
            profiles=list(d.get("profiles") or []),
            db_row_counts=dict(db.get("row_counts") or {}),
            files_approx_bytes=int(files.get("approx_total_bytes") or 0),
            browser_profiles_included=bool(files.get("browser_profiles_included", True)),
            encrypted=bool(d.get("encrypted", False)),
            created_at=d.get("created_at", ""),
            format=d.get("format", BACKUP_FORMAT),
            format_version=int(d.get("format_version") or 0),
        )

    def summary(self) -> dict[str, Any]:
        """A compact, UI-friendly subset (no path internals)."""
        return {
            "app_version": self.app_version,
            "db_provider": self.db_provider,
            "platform": self.platform,
            "hostname": self.hostname,
            "created_at": self.created_at,
            "profiles": list(self.profiles),
            "encrypted": self.encrypted,
            "alembic_revision": self.alembic_revision,
            "db_row_total": sum(self.db_row_counts.values()) if self.db_row_counts else 0,
        }


def now_iso() -> str:
    """UTC ISO-8601 timestamp. Uses time.gmtime (Date.now-free)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def assert_restorable(manifest: Manifest) -> None:
    """Raise :class:`BackupIncompatibleError` if this build can't restore it.

    Refuses, in order:
      - an unrecognised format string;
      - a format version newer than this build understands;
      - an app version newer than the running build (a forward restore would
        expect schema/behaviour this code doesn't have);
      - an app version older than ``MIN_SUPPORTED_UPGRADE_FROM``;
      - an Alembic revision this build's migration tree doesn't know (which is
        the precise "the dump is from a newer schema" test).
    """
    if manifest.format != BACKUP_FORMAT:
        raise BackupIncompatibleError(
            f"Unrecognised backup format {manifest.format!r} (expected {BACKUP_FORMAT!r})."
        )
    if manifest.format_version > BACKUP_FORMAT_VERSION:
        raise BackupIncompatibleError(
            f"Backup format version {manifest.format_version} is newer than this "
            f"build supports ({BACKUP_FORMAT_VERSION}). Upgrade Cremind, then restore."
        )

    from app.__version__ import MIN_SUPPORTED_UPGRADE_FROM
    from app.__version__ import __version__ as CURRENT_VERSION

    try:
        from app.upgrade.manifest import is_at_or_above, is_newer
    except ImportError:
        is_at_or_above = None
        is_newer = None

    if is_newer is not None and manifest.app_version and is_newer(manifest.app_version, CURRENT_VERSION):
        raise BackupIncompatibleError(
            f"Backup was created by Cremind {manifest.app_version}, which is newer "
            f"than this build ({CURRENT_VERSION}). Upgrade Cremind, then restore."
        )
    if (
        is_at_or_above is not None
        and manifest.app_version
        and not is_at_or_above(manifest.app_version, MIN_SUPPORTED_UPGRADE_FROM)
    ):
        raise BackupIncompatibleError(
            f"Backup was created by Cremind {manifest.app_version}, older than the "
            f"minimum this build can restore ({MIN_SUPPORTED_UPGRADE_FROM})."
        )

    rev = manifest.alembic_revision
    if rev:
        try:
            from alembic.script import ScriptDirectory

            from app.storage.migrations import build_alembic_config

            ScriptDirectory.from_config(build_alembic_config()).get_revision(rev)
        except BackupIncompatibleError:
            raise
        except Exception as e:  # noqa: BLE001 — unknown revision or missing alembic
            raise BackupIncompatibleError(
                f"Backup's database revision {rev!r} is unknown to this build "
                f"(it is probably from a newer Cremind). Upgrade Cremind, then restore."
            ) from e


__all__ = [
    "ARCHIVE_SUFFIX",
    "BACKUP_FORMAT",
    "BACKUP_FORMAT_VERSION",
    "DB_MEMBER",
    "FILES_PREFIX",
    "INVENTORY_MEMBER",
    "MANIFEST_MEMBER",
    "BackupError",
    "BackupIncompatibleError",
    "BackupPassphraseError",
    "Manifest",
    "SourcePaths",
    "assert_restorable",
    "now_iso",
]
