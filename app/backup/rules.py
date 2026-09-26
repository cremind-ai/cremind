"""Inclusion/exclusion rules for the file trees under ``CREMIND_SYSTEM_DIR``.

Include-list driven (safer than exclude-driven for a directory that also holds
venvs, caches, and the DB): only the shared ``browser-profile/``, each
``<profile>/`` tree named in the DB, and each profile's own Cremind manual
pages (``storage/cremind_documents/profiles/<uuid>``, keyed by the profile's
uuid — see :func:`include_roots`) are walked. The ``tokens/`` tree (JWT
session tokens) is intentionally excluded. Within those roots, transient and
rebuildable content is pruned:

- the relational DB (``storage/``) is captured as a portable logical dump, not
  copied file-for-file; ``storage/chroma`` embeddings rebuild on boot
- the shared manual (``storage/cremind_documents/shared``) is re-seeded from
  the bundle on every boot, so it is never archived
- ``uploads_tmp/`` is wiped on boot; skill ``scripts/.env`` is regenerated from
  ``tool_configs`` rows; exec_shell stdout dirs are process scratch
- browser-profile Chromium caches are recreatable (login state — cookies,
  Local Storage — is kept)
- ``storage/documents`` and ``storage/userdocs`` (Documentation search index
  files, rebuilt from the user's own folder) and ``.cache`` (downloaded
  embedding models) are pruned by name as well: a profile may be *called*
  ``storage``, and its tree then starts where these live

An archive made before the manual moved carries a profile's pages at
``<profile>/documents``; the restore copies them back there and the relocation
that follows (app/documents/relocate.py) moves them to the uuid directory.

The profiles' working directories are walked separately
(:func:`iter_workspace_files`): the workspaces root holds user files, not
Cremind state, so only the dependency/virtualenv directory prunes apply there —
never the suffix prunes, which would silently drop a ``poetry.lock`` or a
``notes.tmp`` the user wrote.

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

# Top-level trees of the system dir that are never user data. The include
# list alone keeps them out only while no profile shares their first
# component: profile names are ``[a-z0-9_-]+``, so a profile named
# ``storage`` makes the walk start at ``<SYSTEM_DIR>/storage/``. Each is a
# path prefix from the system dir; everything under it is pruned.
_TOP_LEVEL_EXCLUDES: tuple[tuple[str, ...], ...] = (
    # Documentation search index files (can reach gigabytes); a restore
    # re-indexes the user's folder instead. ``storage/userdocs`` is where
    # they lived before the rename — excluded until every install has moved
    # them (app/documents/relocate.py).
    ("storage", "documents"),
    ("storage", "userdocs"),
    # The bundled manual's mirror, re-seeded on every boot.
    ("storage", "cremind_documents", "shared"),
    # The relocation's journal and lock: bookkeeping of THIS installation's
    # move, meaningless (and misleading) on another.
    ("storage", "document-relocation.json"),
    ("storage", "document-relocation.lock"),
    # HF_HOME / SENTENCE_TRANSFORMERS_HOME in the container image: embedding
    # models, downloaded again on first use.
    (".cache",),
)

# Each profile's own manual pages: ``storage/cremind_documents/profiles/<uuid>``
# is walked as a root of its own (a uuid, not a name, so it needs the profile
# ids — see :func:`include_roots`). Mirrors app.cremind_documents.paths.
AUTHORED_DOCS_PARTS: tuple[str, ...] = ("storage", "cremind_documents", "profiles")


def authored_docs_root(uid: str) -> str:
    """The archive-relative root of one profile's manual pages."""
    return "/".join((*AUTHORED_DOCS_PARTS, uid))


def _valid_uid(uid: str) -> bool:
    s = str(uid or "")
    return bool(s) and s not in (".", "..") and not any(c in s for c in ("/", "\\", "\0"))


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


def include_roots(
    profiles: list[str],
    *,
    include_browser_profiles: bool = True,
    profile_uids: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """Relative roots to walk, in a stable order.

    ``tokens/`` (per-profile JWT session tokens) is deliberately **not** walked:
    the JWT signing secret and its issued tokens are local to an installation —
    carrying them across a restore breaks auth (they'd be verified against a
    different secret). Restore re-mints these files under the target's own secret
    (see ``app/backup/engine.py``). Per-profile OAuth tokens (e.g. a skill's
    ``scripts/.google_token.json``) live under the ``<profile>/`` root and are
    still backed up as user data.

    ``profile_uids`` adds each profile's manual pages
    (``storage/cremind_documents/profiles/<uuid>``) — user-written content
    that lives outside the profile's name-keyed tree. Only the uuids of live
    profiles are passed, so a deleted profile's leftovers are never archived.
    """
    roots: list[str] = []
    if include_browser_profiles:
        roots.append("browser-profile")
    roots.extend(sorted(profiles))
    roots.extend(authored_docs_root(uid) for uid in sorted(set(profile_uids)) if _valid_uid(uid))
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

    if any(tuple(parts[: len(prefix)]) == prefix for prefix in _TOP_LEVEL_EXCLUDES):
        return True

    # The manual pages' own roots are walked explicitly; below them only the
    # global prunes (``__pycache__``, ``*.tmp``…) apply — never the per-profile
    # layout rules, which would misread ``storage`` as a profile name.
    if tuple(parts[: len(AUTHORED_DOCS_PARTS)]) == AUTHORED_DOCS_PARTS:
        if any(comp in _GLOBAL_EXCLUDE_DIRS for comp in parts):
            return True
        return not is_dir and name.lower().endswith(_GLOBAL_EXCLUDE_FILE_SUFFIXES)

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
    profile_uids: list[str] | tuple[str, ...] = (),
) -> Iterator[tuple[str, str]]:
    """Yield ``(absolute_source_path, relative_posix_arcname)`` for each file.

    ``relative_posix_arcname`` is relative to ``system_dir`` (the engine prepends
    the ``files/`` member prefix). Directories are pruned in-place so excluded
    subtrees are never descended. Symlinks are skipped (the caller records them).

    Each file is yielded once even when two roots overlap (a profile named
    ``storage`` walks ``<SYS>/storage``, which contains the manual roots). A
    manual directory whose uuid is not in ``profile_uids`` — a deleted
    profile's leftovers — is never walked, from any root.
    """
    base = Path(system_dir)
    live_uids = {u for u in profile_uids if _valid_uid(u)}
    seen: set[str] = set()
    for root in include_roots(
        profiles, include_browser_profiles=include_browser_profiles, profile_uids=list(live_uids),
    ):
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
                if rel_dir_posix == "/".join(AUTHORED_DOCS_PARTS) and d not in live_uids:
                    continue  # a deleted profile's manual pages
                kept.append(d)
            dirnames[:] = kept

            for f in filenames:
                child_rel = f"{rel_dir_posix}/{f}" if rel_dir_posix else f
                full = os.path.join(dirpath, f)
                if os.path.islink(full):
                    continue
                if is_excluded(child_rel, is_dir=False):
                    continue
                if child_rel in seen:
                    continue
                seen.add(child_rel)
                yield full, child_rel


def _norm_abs(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def iter_workspace_files(
    workspaces_root: str,
    *,
    exclude_dirs: list[str] | tuple[str, ...] = (),
    exclude_files: list[str] | tuple[str, ...] = (),
) -> Iterator[tuple[str, str]]:
    """Yield ``(absolute_source_path, relative_posix_name)`` for every file
    under the workspaces root — each profile's default working directory and
    the ``.deleted`` folders of deleted profiles, all of it user content.

    ``relative_posix_name`` is relative to ``workspaces_root`` (the engine
    prepends the ``workspaces/`` member prefix). Only the dependency and
    virtualenv directories are pruned (``node_modules``, ``.venv``, ``venv``,
    ``__pycache__``); file suffixes are never — a ``poetry.lock`` is the
    user's. Symlinks are skipped, as in :func:`iter_backup_files`.

    ``exclude_dirs`` are absolute directories never descended into: the
    engine passes the system dir, so a workspaces root that happens to
    contain it never archives the database, the tokens or the backups folder
    through the back door. ``exclude_files`` are absolute files skipped (the
    archive being written, should it land inside the root).
    """
    root = os.path.abspath(workspaces_root)
    if not os.path.isdir(root):
        return
    pruned = {_norm_abs(d) for d in exclude_dirs if d}
    skipped = {_norm_abs(f) for f in exclude_files if f}
    if _norm_abs(root) in pruned:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in _GLOBAL_EXCLUDE_DIRS or os.path.islink(full):
                continue
            if _norm_abs(full) in pruned:
                continue
            kept.append(d)
        dirnames[:] = kept
        for f in filenames:
            full = os.path.join(dirpath, f)
            if os.path.islink(full) or _norm_abs(full) in skipped:
                continue
            try:
                rel = os.path.relpath(full, root)
            except ValueError:
                continue
            yield full, rel.replace(os.sep, "/")


__all__ = [
    "AUTHORED_DOCS_PARTS",
    "authored_docs_root",
    "include_roots",
    "is_excluded",
    "iter_backup_files",
    "iter_workspace_files",
    "long_path",
]
