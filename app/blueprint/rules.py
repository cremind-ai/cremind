"""Inclusion/exclusion rules for bundling a skill's file tree into a blueprint.

A blueprint bundles the *files* of non-built-in skills so an importer gets the
skill itself, not just its config. The filter here is deliberately **stricter
than the backup file walk** (:mod:`app.backup.rules`): a backup restores onto
the same owner, so it keeps a skill's OAuth token files; a blueprint is shared
with *other people*, so every credential-bearing file must be dropped.

Excluded, at any depth inside a skill dir:

- any dot-prefixed file or directory — kills ``scripts/.env``,
  ``scripts/.google_token.json``, ``scripts/.atlassian_token.json``,
  ``scripts/.ha_token.json``, ``scripts/.listener_state.json``, ``.git``,
  ``.venv``, ``.DS_Store`` in one rule
- case-insensitive credential globs (``*token*.json``, ``*secret*``,
  ``*credential*``, ``*password*``) — a belt-and-braces catch for skills that
  write a non-dotfile credential store
- rebuildable/junk dirs (``__pycache__``, ``.venv``, ``venv``, ``node_modules``)
  and file suffixes (``.pyc``, ``.pyo``, ``.lock``, ``.tmp``)
- the contents of ``events/<type>/`` drop-zone directories — those are runtime
  event payloads, not design; the importer recreates the (empty) dirs

Pure functions — no ``app.*`` imports — so this is trivially unit-testable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

# Directory names pruned at any depth.
_EXCLUDE_DIRS = frozenset({"__pycache__", ".venv", "venv", "node_modules"})

# File name suffixes pruned at any depth.
_EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo", ".lock", ".tmp")

# Credential-bearing filename patterns (case-insensitive), matched against the
# basename. Dotfiles are already excluded wholesale; these catch a skill that
# writes e.g. ``my_token.json`` or ``creds.secret`` without a leading dot.
_CREDENTIAL_GLOBS = re.compile(
    r"(?i)(token|secret|credential|password)",
)


def is_skill_file_excluded(rel_posix: str, *, is_dir: bool) -> bool:
    """Whether a path (relative to the skill dir, POSIX-separated) is excluded.

    ``rel_posix`` is relative to the skill's own directory, e.g.
    ``scripts/app/client.py`` or ``events/new_mail``.
    """
    parts = [p for p in rel_posix.split("/") if p]
    if not parts:
        return False
    name = parts[-1]

    # Any dot-prefixed component (file or dir) at any depth.
    if any(comp.startswith(".") for comp in parts):
        return True

    # Rebuildable/junk directories at any depth.
    if any(comp in _EXCLUDE_DIRS for comp in parts):
        return True

    # events/<type>/<file> — drop-zone payloads (runtime data). The ``events``
    # dir itself and its ``<type>`` subdirs are kept so the importer preserves
    # the declared structure; only files two-or-more levels deep are dropped.
    if parts[0] == "events" and len(parts) >= 3 and not is_dir:
        return True

    if not is_dir:
        lower = name.lower()
        if lower.endswith(_EXCLUDE_FILE_SUFFIXES):
            return True
        if _CREDENTIAL_GLOBS.search(name):
            return True

    return False


def is_credential_member(basename: str) -> bool:
    """Fail-closed check used by the export audit: does a basename look like a
    credential store (dotfile or credential glob)?"""
    if basename.startswith("."):
        return True
    return bool(_CREDENTIAL_GLOBS.search(basename))


def iter_skill_files(skill_dir: str) -> Iterator[tuple[str, str]]:
    """Yield ``(absolute_source_path, relative_posix_path)`` for each kept file.

    ``relative_posix_path`` is relative to ``skill_dir`` itself. Directories are
    pruned in-place so excluded subtrees are never descended. Symlinks are
    skipped (never legitimately part of a skill and a traversal vector).
    """
    base = Path(skill_dir)
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(str(base)):
        try:
            rel_dir = os.path.relpath(dirpath, str(base))
        except ValueError:
            continue
        rel_dir_posix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

        kept_dirs = []
        for d in dirnames:
            child_rel = f"{rel_dir_posix}/{d}" if rel_dir_posix else d
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                continue
            if is_skill_file_excluded(child_rel, is_dir=True):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for f in filenames:
            child_rel = f"{rel_dir_posix}/{f}" if rel_dir_posix else f
            full = os.path.join(dirpath, f)
            if os.path.islink(full):
                continue
            if is_skill_file_excluded(child_rel, is_dir=False):
                continue
            yield full, child_rel


__all__ = [
    "is_credential_member",
    "is_skill_file_excluded",
    "iter_skill_files",
]
