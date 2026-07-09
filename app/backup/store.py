"""Backups-directory helpers: list, resolve (traversal-safe), and name backups.

Full-system archives (``*.cremind-backup``) share ``<sys>/backups/`` with the
raw ``cremind db backup`` snapshots (``*.sqlite.gz`` / ``*.pgsnap.gz``), so
listing filters by the archive suffix.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.backup.manifest import ARCHIVE_SUFFIX

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def backups_root() -> Path:
    from app.config.settings import BaseConfig

    root = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_safe_name(name: str) -> bool:
    return bool(name) and bool(_SAFE_NAME_RE.match(name)) and "/" not in name and "\\" not in name


def resolve_archive(name: str) -> Path:
    """Resolve ``name`` strictly under the backups dir (rejects traversal)."""
    if not is_safe_name(name):
        raise ValueError(f"Unsafe backup name: {name!r}")
    root = backups_root().resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise ValueError(f"Backup name escapes the backups directory: {name!r}")
    return target


def safe_upload_name(filename: str) -> str:
    """Sanitise an uploaded filename to a safe basename ending in the suffix."""
    base = Path(filename or "").name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    if not base.endswith(ARCHIVE_SUFFIX):
        base += ARCHIVE_SUFFIX
    return base


def list_archives() -> list[dict[str, Any]]:
    """Return archive descriptors (name/size/created), newest first.

    Manifest summaries are attached by the API layer (which owns the engine
    import) — here we only stat the files.
    """
    root = backups_root()
    out: list[dict[str, Any]] = []
    for p in root.glob(f"*{ARCHIVE_SUFFIX}"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"name": p.name, "size_bytes": st.st_size, "created_at": st.st_mtime})
    out.sort(key=lambda d: d["created_at"], reverse=True)
    return out


__all__ = [
    "backups_root",
    "is_safe_name",
    "list_archives",
    "resolve_archive",
    "safe_upload_name",
]
