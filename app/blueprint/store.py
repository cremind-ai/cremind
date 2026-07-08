"""Blueprints-directory + import-staging helpers (traversal-safe).

Exported ``.cremind-blueprint`` archives live under ``<sys>/blueprints/``; an
import session's staged payload lives under
``<sys>/blueprints/staging/<session-id>/``. This module owns the paths and the
name/traversal guards, mirroring :mod:`app.backup.store`.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.blueprint.manifest import ARCHIVE_SUFFIX

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SESSION_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")

# Staged import sessions older than this are swept (on boot and on new upload).
SESSION_TTL_S = 24 * 3600


def blueprints_root() -> Path:
    from app.config.settings import BaseConfig

    root = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "blueprints"
    root.mkdir(parents=True, exist_ok=True)
    return root


def staging_root() -> Path:
    root = blueprints_root() / "staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_safe_name(name: str) -> bool:
    return bool(name) and bool(_SAFE_NAME_RE.match(name)) and "/" not in name and "\\" not in name


def resolve_archive(name: str) -> Path:
    """Resolve ``name`` strictly under the blueprints dir (rejects traversal)."""
    if not is_safe_name(name):
        raise ValueError(f"Unsafe blueprint name: {name!r}")
    root = blueprints_root().resolve()
    target = (root / name).resolve()
    if target.parent != root:
        raise ValueError(f"Blueprint name escapes the blueprints directory: {name!r}")
    return target


def safe_upload_name(filename: str) -> str:
    base = Path(filename or "").name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    if not base.endswith(ARCHIVE_SUFFIX):
        base += ARCHIVE_SUFFIX
    return base


def slug_filename(name: str) -> str:
    """A safe archive basename slug (no suffix, no timestamp) from a display name."""
    base = re.sub(r"[^A-Za-z0-9._-]", "-", (name or "").strip()).strip("-._")
    return base.lower() or "blueprint"


def session_dir(session_id: str) -> Path:
    if not _SESSION_ID_RE.match(session_id or ""):
        raise ValueError(f"Unsafe session id: {session_id!r}")
    root = staging_root().resolve()
    target = (root / session_id).resolve()
    if target.parent != root:
        raise ValueError(f"Session id escapes the staging directory: {session_id!r}")
    return target


def list_archives() -> list[dict[str, Any]]:
    """Return archive descriptors (name/size/created), newest first.

    Manifest summaries are attached by the API layer (which owns the engine
    import) — here we only stat the files.
    """
    root = blueprints_root()
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


def sweep_stale_sessions(*, ttl_s: float = SESSION_TTL_S) -> int:
    """Delete staged import sessions whose ``session.json`` is older than ``ttl_s``.

    Called on server boot and on each new upload so abandoned imports don't
    accumulate. Returns the number of session dirs removed.
    """
    root = staging_root()
    removed = 0
    now = time.time()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        marker = child / "session.json"
        try:
            mtime = marker.stat().st_mtime if marker.is_file() else child.stat().st_mtime
        except OSError:
            continue
        if now - mtime > ttl_s:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


__all__ = [
    "SESSION_TTL_S",
    "blueprints_root",
    "is_safe_name",
    "list_archives",
    "resolve_archive",
    "safe_upload_name",
    "session_dir",
    "slug_filename",
    "staging_root",
    "sweep_stale_sessions",
]
