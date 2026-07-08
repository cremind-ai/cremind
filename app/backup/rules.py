"""Inclusion/exclusion rules for the file trees under ``CREMIND_SYSTEM_DIR``.

Include-list driven (safer than exclude-driven for a directory that also holds
venvs, caches, and the DB): only the shared ``browser-profile/`` and each
``<profile>/`` tree named in the DB are walked. The ``tokens/`` tree (JWT
session tokens) is intentionally excluded — see :func:`include_roots`. Within
those roots, transient and rebuildable content is pruned:

- the relational DB (``storage/``) is captured as a portable logical dump, not
  copied file-for-file; ``storage/chroma`` embeddings rebuild on boot
- shared ``documents/`` is re-seeded from the bundle on every boot
- ``uploads_tmp/`` is wiped on boot; skill ``scripts/.env`` is regenerated from
  ``tool_configs`` rows; exec_shell stdout dirs are process scratch
- browser-profile Chromium caches are recreatable (login state — cookies,
  Local Storage — is kept)

Pure functions only — no ``app.*`` imports — so this is trivially unit-testable
and safe to import from the offline CLI.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

# Directory names pruned at any depth.
_GLOBAL_EXCLUDE_DIRS = frozenset({"__pycache__", ".venv", "venv", "node_modules"})

# File name suffixes pruned at any depth.
_GLOBAL_EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo", ".lock", ".tmp")

# Chromium cache directories, pruned only inside a ``browser-profile`` tree so
# an unrelated user file/dir that happens to share the name is never dropped.
_BROWSER_CACHE_DIRS = frozenset(
    {"Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "CacheStorage"}
)

# <profile>/tools/builtin/exec_shell/stdout/** — process scratch.
_EXEC_SHELL_STDOUT = ("tools", "builtin", "exec_shell", "stdout")


def long_path(p: str) -> str:
    r"""Return a Windows ``\\?\``-prefixed absolute path for deep trees.

    No-op off Windows and for already-prefixed / non-absolute paths. Browser
    profiles routinely exceed the 260-char ``MAX_PATH`` limit, so file reads go
    through this. Archive member names are relative and unaffected.
    """
    if os.name != "nt":
        return p
    if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        return p
    ap = os.path.abspath(p)
    if ap.startswith("\\\\"):  # UNC path
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def include_roots(profiles: list[str], *, include_browser_profiles: bool = True) -> list[str]:
    """Top-level relative roots to walk, in a stable order.

    ``tokens/`` (per-profile JWT session tokens) is deliberately **not** walked:
    the JWT signing secret and its issued tokens are local to an installation —
    carrying them across a restore breaks auth (they'd be verified against a
    different secret). Restore re-mints these files under the target's own secret
    (see ``app/backup/engine.py``). Per-profile OAuth tokens (e.g. a skill's
    ``scripts/.google_token.json``) live under the ``<profile>/`` root and are
    still backed up as user data.
    """
    roots: list[str] = []
    if include_browser_profiles:
        roots.append("browser-profile")
    roots.extend(sorted(profiles))
    return roots


def _rel_parts(rel_posix: str) -> list[str]:
    return [p for p in rel_posix.split("/") if p]


def is_excluded(rel_posix: str, *, is_dir: bool) -> bool:
    """Whether a path (relative to system dir, POSIX-separated) is excluded.

    ``rel_posix`` starts at a top-level include root (e.g. ``admin/skills/...``).
    """
    parts = _rel_parts(rel_posix)
    if not parts:
        return False
    name = parts[-1]

    # Global directory prunes at any depth.
    if any(comp in _GLOBAL_EXCLUDE_DIRS for comp in parts):
        return True

    # Browser-profile cache prunes (only within a browser-profile tree).
    if "browser-profile" in parts and any(c in _BROWSER_CACHE_DIRS for c in parts):
        return True

    if not is_dir and name.lower().endswith(_GLOBAL_EXCLUDE_FILE_SUFFIXES):
        return True

    # Per-profile relative excludes. ``parts[0]`` is the profile (or a shared
    # top-level root); the subtree layout is what these match.
    if len(parts) >= 2 and parts[1] in ("uploads_tmp", "oauth_inbox"):
        return True

    # <profile>/skills/<skill>/scripts/.env — regenerated from tool_configs.
    if (
        len(parts) >= 5
        and parts[1] == "skills"
        and parts[3] == "scripts"
        and name == ".env"
    ):
        return True

    # <profile>/tools/builtin/exec_shell/stdout/**
    if len(parts) >= 5 and tuple(parts[1:5]) == _EXEC_SHELL_STDOUT:
        return True

    return False


def iter_backup_files(
    system_dir: str,
    profiles: list[str],
    *,
    include_browser_profiles: bool = True,
) -> Iterator[tuple[str, str]]:
    """Yield ``(absolute_source_path, relative_posix_arcname)`` for each file.

    ``relative_posix_arcname`` is relative to ``system_dir`` (the engine prepends
    the ``files/`` member prefix). Directories are pruned in-place so excluded
    subtrees are never descended. Symlinks are skipped (the caller records them).
    """
    base = Path(system_dir)
    for root in include_roots(profiles, include_browser_profiles=include_browser_profiles):
        start = base / root
        if not start.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(str(start)):
            try:
                rel_dir = os.path.relpath(dirpath, str(base))
            except ValueError:
                continue
            rel_dir_posix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

            # Prune excluded subdirectories in-place (topdown default).
            kept = []
            for d in dirnames:
                child_rel = f"{rel_dir_posix}/{d}" if rel_dir_posix else d
                if os.path.islink(os.path.join(dirpath, d)):
                    continue  # don't descend symlinked dirs
                if is_excluded(child_rel, is_dir=True):
                    continue
                kept.append(d)
            dirnames[:] = kept

            for f in filenames:
                child_rel = f"{rel_dir_posix}/{f}" if rel_dir_posix else f
                full = os.path.join(dirpath, f)
                if os.path.islink(full):
                    continue
                if is_excluded(child_rel, is_dir=False):
                    continue
                yield full, child_rel


__all__ = [
    "include_roots",
    "is_excluded",
    "iter_backup_files",
    "long_path",
]
