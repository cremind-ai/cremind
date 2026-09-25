"""The full reconcile: walk the root, compare with the manifest, report what changed.

Runs at boot (catching everything that changed while Cremind was off), every
few hours, on wake from sleep, and as *the* change detector when native
watching is unavailable (Docker Desktop bind mounts, network shares). It is a
pure function of the filesystem and the manifest handed in — no database — so
the caller decides what to write and when.

Change detection is by fingerprint only: size, ``mtime_ns`` and inode. Content
is hashed only to pair up moves (see :mod:`.moves`), never to confirm an
unchanged file — reading 100k files to learn that nothing happened is the cost
this design exists to avoid.

**"Missing" is a claim this function only makes when it is sure.** A file is
missing only if the walk finished *and* could list the directory it lived in.
A capped or stopped walk reports no missing files at all, and a directory that
could not be listed (permission denied, a flaky share) protects every row
under it. Deleting a user's index because a network drive hiccupped is the
failure mode; the :class:`~.guard.RootGuard` thresholds sit on top of this.
"""

from __future__ import annotations

import functools
import threading
from dataclasses import dataclass, field
from typing import Callable

from app.documents.discovery.hashing import sha256_file
from app.documents.discovery.ignore import SKIP, IgnoreMatcher, fold_case
from app.documents.discovery.moves import match_moves
from app.documents.discovery.walker import path_hash, walk
from app.documents.types import FsEntry, ManifestRow

# How often (in files) ``on_progress`` is called.
_PROGRESS_EVERY = 500

# A manifest row the engine has already marked as gone (hidden, awaiting a
# mass-delete confirmation or its 14-day recheck).
_STATUS_MISSING = "missing"


@dataclass
class ScanResult:
    # Files the manifest does not know (after moves are taken out).
    new: list[FsEntry] = field(default_factory=list)
    # Same path, different size / mtime_ns / inode: re-extract.
    changed: list[tuple[ManifestRow, FsEntry]] = field(default_factory=list)
    # Rows whose file is gone (after moves are taken out). Empty when the walk
    # was incomplete (``truncated``).
    missing: list[ManifestRow] = field(default_factory=list)
    # Same file under a new path: update the row, re-diff only the file card.
    # Also carries case-only renames on case-insensitive filesystems, where the
    # manifest key is unchanged but ``rel_path`` is not.
    moves: list[tuple[ManifestRow, FsEntry]] = field(default_factory=list)
    # Every directory the walk entered (for folder rows).
    dirs: list[FsEntry] = field(default_factory=list)
    seen_files: int = 0
    # The walk did not cover the whole tree: the ``max_entries`` cap was hit
    # or ``stop`` was set. ``missing``, ``excluded`` and ``moves`` are empty.
    truncated: bool = False
    # Human-readable "path: error" lines for directories/entries skipped.
    errors: list[str] = field(default_factory=list)
    # ``truncated`` because ``stop`` was set (shutdown/pause), not the cap —
    # so the UI does not report "limit reached".
    stopped: bool = False
    # Rows not seen because the matcher now excludes their path (a new
    # exclude or .cremindignore rule). Out of scope, not "disappeared": they
    # are purged without the mass-delete confirmation, which exists for files
    # that vanished on their own.
    excluded: list[ManifestRow] = field(default_factory=list)
    # Rows marked ``missing`` that are back at the same path, unchanged:
    # restore them (no re-extraction). A returned file that did change is in
    # ``changed`` instead.
    returned: list[tuple[ManifestRow, FsEntry]] = field(default_factory=list)


def _under(rel: str, dir_keys: set[str]) -> bool:
    """``rel`` equals or lies below one of ``dir_keys`` ("" = the root)."""
    if "" in dir_keys:
        return True
    key = fold_case(rel)
    if key in dir_keys:
        return True
    while True:
        key, sep, _ = key.rpartition("/")
        if not sep:
            return False
        if key in dir_keys:
            return True


def scan_diff(
    root_abs: str,
    matcher: IgnoreMatcher,
    manifest: dict[str, ManifestRow],
    *,
    hasher: Callable[[str], str | None] = sha256_file,
    stop: threading.Event | None = None,
    max_entries: int = 500_000,
    on_progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """Walk ``root_abs`` and diff it against ``manifest`` (keyed by
    :func:`~.walker.path_hash` of ``rel_path``).

    ``hasher`` is only used for move matching; it must produce what the
    manifest's ``sha256`` holds. ``on_progress(seen_files)`` is called every
    few hundred files. The matcher's cached ``.cremindignore`` rules are
    re-read first, so a full scan always reflects the files on disk.
    """
    matcher.invalidate()
    result = ScanResult()
    error_dirs: set[str] = set()

    def on_error(rel: str, exc: BaseException) -> None:
        result.errors.append(f"{rel or '.'}: {exc}")
        if isinstance(exc, OSError):
            # A directory we could not list, or a file we could not stat:
            # that path and everything under it is unknown, not gone.
            error_dirs.add(fold_case(rel))

    seen: set[str] = set()
    entries = 0
    for entry in walk(root_abs, matcher, stop=stop, max_entries=max_entries + 1, on_error=on_error):
        entries += 1
        if entries > max_entries:
            result.truncated = True
            break
        if entry.is_dir:
            result.dirs.append(entry)
            continue
        result.seen_files += 1
        if on_progress is not None and result.seen_files % _PROGRESS_EVERY == 0:
            try:
                on_progress(result.seen_files)
            except Exception:  # noqa: BLE001 — progress reporting must not end a scan
                pass
        key = path_hash(entry.rel_path)
        row = manifest.get(key)
        if row is None:
            result.new.append(entry)
            continue
        seen.add(key)
        fingerprint_changed = (
            row.size != entry.size
            or row.mtime_ns != entry.mtime_ns
            # Inode only when both sides know it: placeholders and rows written
            # before inodes were recorded carry 0.
            or (bool(row.ino) and bool(entry.ino) and row.ino != entry.ino)
        )
        if fingerprint_changed:
            result.changed.append((row, entry))
        elif row.status == _STATUS_MISSING:
            result.returned.append((row, entry))
        elif row.rel_path != entry.rel_path:
            result.moves.append((row, entry))  # case-only rename
    if stop is not None and stop.is_set():
        result.truncated = result.stopped = True
    if on_progress is not None:
        try:
            on_progress(result.seen_files)
        except Exception:  # noqa: BLE001
            pass

    if result.truncated:
        # Unseen is not gone when we did not look everywhere.
        return result

    candidates: list[ManifestRow] = []
    for key, row in manifest.items():
        if key in seen or _under(row.rel_path, error_dirs):
            continue
        if matcher.classify(row.rel_path, None) == SKIP:
            result.excluded.append(row)
        else:
            candidates.append(row)

    move_hasher = functools.partial(sha256_file, stop=stop) if hasher is sha256_file else hasher
    pairs = match_moves(candidates, result.new, hasher=move_hasher)
    if pairs:
        by_id = {row.id: row for row in candidates}
        moved_entries = {id(entry) for _, entry in pairs}
        moved_rows = {row_id for row_id, _ in pairs}
        result.moves.extend((by_id[row_id], entry) for row_id, entry in pairs)
        result.new = [e for e in result.new if id(e) not in moved_entries]
        candidates = [row for row in candidates if row.id not in moved_rows]
    result.missing = candidates
    return result


__all__ = ["ScanResult", "scan_diff"]
