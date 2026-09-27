"""Index lookups behind the file tree's status buttons and the preview.

- :func:`lookup_paths` — which of the paths a file tree shows are in this
  profile's index, and what each holds (the content summary of
  :mod:`app.documents.content`), or why it is not there: outside the indexed
  folder, excluded, not indexed yet.
- :func:`file_preview` — one page of a file's stored content.
- :func:`summaries` — the summary of rows a listing already read.

Everything here only *reads* the index: opening a preview or refreshing the
tree never extracts, transcribes, embeds or calls a model.

**Isolation.** The profile is always the caller's own. A path is looked up
only under the profile's own indexed folder, after resolving links: a path
inside another profile's working directory, inside Cremind's system folder
(unless the profile's own workspace is there), or that resolves outside the
folder is ``outside`` — the admin included. A conversation's wider file
access never widens what the index shows.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Iterable

from app.documents import content as C
from app.documents import settings as uds
from app.utils.logger import logger

MAX_LOOKUP_PATHS = 500

# Lookup item states for paths that are not (or not visibly) in the index.
OUTSIDE = "outside"
EXCLUDED = "excluded"
UNMATCHED = "unmatched"
INDEXED = "indexed"          # found: the item carries fid and summary
GONE = "gone"


def _runtime(profile: str) -> Any:
    from app.documents.service import get_service

    svc = get_service()
    return svc.runtime(profile) if svc is not None else None


def local_enabled(profile: str) -> bool:
    from app.storage.documents_storage import get_documents_storage

    try:
        row = get_documents_storage().get_source(profile, uds.SOURCE_LOCAL) or {}
    except Exception:  # noqa: BLE001 — no storage: nothing is indexed
        return False
    return bool(row.get("enabled"))


def summaries(db: Any, rows: Iterable[dict[str, Any]], *, in_flight: Iterable[int] = ()) -> dict[int, dict[str, Any]]:
    """``{file id: content summary}`` for rows already read (one chunk query
    for all of them)."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return {}
    busy = {int(i) for i in in_flight}
    gen = C.active_gen(db)
    stats = C.chunk_stats(db, [int(r["id"]) for r in rows], gen=gen)
    return {
        int(r["id"]): C.summarize(r, stats.get(int(r["id"])), in_flight=int(r["id"]) in busy, gen=gen)
        for r in rows
    }


def _in_flight(profile: str) -> set[int]:
    rt = _runtime(profile)
    lock, busy = getattr(rt, "lock", None), getattr(rt, "in_flight", None)
    if lock is None or busy is None:
        return set()
    with lock:
        return set(busy)


def _matcher(profile: str, root: str) -> Any:
    """The running engine's ignore matcher for ``root``, or one built from
    the saved settings (a stopped engine still answers lookups)."""
    rt = _runtime(profile)
    matcher = getattr(rt, "matcher", None)
    rt_root = getattr(rt, "root", None)
    if matcher is not None and rt_root and os.path.normcase(rt_root) == os.path.normcase(root):
        return matcher
    try:
        from app.documents.discovery.ignore import IgnoreMatcher
        from app.storage.documents_storage import get_documents_storage

        row = get_documents_storage().get_source(profile, uds.SOURCE_LOCAL) or {}
        check = uds.validate_root(profile)
        return IgnoreMatcher(
            root, excludes=uds.normalize_excludes(row.get("excludes")),
            locked_excludes=list(check.locked_excludes) if check.ok else [],
            system_dir=uds.system_dir(), root_sanctioned=uds.system_dir_exempt(root),
        )
    except Exception as exc:  # noqa: BLE001 — no matcher: the index row decides
        logger.debug(f"[documents] {profile}: no ignore matcher for lookups: {exc}")
        return None


def _rel_in_root(path: str, root: str, root_real: str, profile: str) -> tuple[str | None, str]:
    """``(rel path, "")`` of ``path`` inside the indexed folder, or ``(None,
    why)``. The path must lie in the folder as written *and* resolve inside
    it — a link planted in the folder that points elsewhere is outside —
    and must not be in another profile's working directory or in the system
    folder (unless the root itself is the profile's workspace there)."""
    from app.config import working_dirs

    if not path or not os.path.isabs(path):
        return None, "not_absolute"
    norm = os.path.normpath(path)
    try:
        real = uds.real_path(norm)
    except (OSError, ValueError):
        real = norm
    if working_dirs.is_foreign(real, profile) or working_dirs.is_foreign(norm, profile):
        return None, "foreign"
    if uds.is_inside(real, uds.system_dir()) and not uds.system_dir_exempt(root_real):
        return None, "system"
    if uds.is_inside(norm, root):
        rel = os.path.relpath(norm, root)
    elif uds.is_inside(real, root_real):
        rel = os.path.relpath(real, root_real)
    else:
        return None, "outside_root"
    if not uds.is_inside(real, root_real):
        return None, "outside_root"
    rel = unicodedata.normalize("NFC", rel.replace(os.sep, "/").replace("\\", "/"))
    if rel in (".", ""):
        return None, "root"
    return rel, ""


def lookup_paths(profile: str, paths: list[str]) -> dict[str, Any]:
    """The index entry of each path (see the module docstring).

    Returns ``{"enabled", "root", "items": {path: item}}``. An item is
    ``{"state": "indexed", "fid", "name", "kind", "status", "summary"}`` for
    an indexed file, else ``{"state": outside | excluded | unmatched | gone,
    "reason"}``. With the local folder off, ``enabled`` is false and
    ``items`` is empty: the tree shows no index status at all."""
    from app.api.documents_files import _local_root
    from app.documents.citations import profile_index
    from app.documents.discovery.ignore import SKIP
    from app.documents.discovery.walker import path_hash
    from app.documents.query.filters import hidden_sources

    paths = [p for p in dict.fromkeys(str(p) for p in paths if isinstance(p, str) and p)][:MAX_LOOKUP_PATHS]
    if not local_enabled(profile):
        return {"enabled": False, "root": None, "items": {}}
    root = _local_root(profile)
    if not root:
        return {"enabled": True, "root": None, "available": False, "items": {
            p: {"state": UNMATCHED, "reason": "root_unavailable"} for p in paths}}
    root_real = uds.real_path(root)
    items: dict[str, dict[str, Any]] = {}
    found: dict[str, dict[str, Any]] = {}
    matcher = _matcher(profile, root)
    with profile_index(profile) as db:
        if db is None:
            return {"enabled": True, "root": root, "available": False, "items": {
                p: {"state": UNMATCHED, "reason": "no_index"} for p in paths}}
        hidden = hidden_sources(db)
        for p in paths:
            rel, why = _rel_in_root(p, root, root_real, profile)
            if rel is None:
                items[p] = {"state": OUTSIDE, "reason": why}
                continue
            abs_path = os.path.join(root, *rel.split("/"))
            if matcher is not None:
                try:
                    if matcher.classify(rel, abs_path) == SKIP:
                        items[p] = {"state": EXCLUDED, "reason": "excluded"}
                        continue
                except Exception:  # noqa: BLE001 — classification failed: the row decides
                    pass
            row = db.file_by_path(uds.SOURCE_LOCAL, path_hash(rel))
            if row is None or row.get("source") in hidden:
                items[p] = {"state": UNMATCHED, "reason": "not_indexed"}
                continue
            if row.get("status") in ("tombstone", "missing"):
                items[p] = {"state": GONE, "reason": row.get("status")}
                continue
            found[p] = row
        sums = summaries(db, found.values(), in_flight=_in_flight(profile))
    for p, row in found.items():
        items[p] = {
            "state": INDEXED,
            "fid": row.get("cite_id"),
            "name": row.get("name"),
            "rel_path": row.get("rel_path"),
            "kind": row.get("kind"),
            "status": row.get("status"),
            "summary": sums.get(int(row["id"])),
        }
    return {"enabled": True, "root": root, "available": True, "items": items}


def file_preview(profile: str, fid: str, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """One preview page of the file ``fid`` in this profile's index. Raises
    :class:`LookupError` for an unknown or hidden file, and
    :class:`~app.documents.content.StalePreview` /
    :class:`~app.documents.content.BadCursor` for a cursor that no longer
    fits."""
    from app.documents.citations import profile_index
    from app.documents.query.filters import hidden_sources

    with profile_index(profile) as db:
        if db is None:
            raise LookupError("No such file.")
        row = db.file_by_cite(str(fid).lower())
        if row is None or row.get("status") in ("tombstone", "missing"):
            raise LookupError("No such file.")
        if row.get("source") in hidden_sources(db):
            raise LookupError("Your Google Drive files are hidden until Google Drive is re-linked.")
        return C.preview_page(db, row, cursor=cursor, limit=limit or C.PREVIEW_DEFAULT_SEGMENTS,
                              in_flight=int(row["id"]) in _in_flight(profile))


__all__ = ["EXCLUDED", "GONE", "INDEXED", "MAX_LOOKUP_PATHS", "OUTSIDE", "UNMATCHED", "file_preview",
           "local_enabled", "lookup_paths", "summaries"]
