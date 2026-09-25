"""Recognising a renamed or moved file, so its index entry moves instead of being rebuilt.

A scan sees a move as two unrelated facts: a path the index knows is gone,
and a path it does not know has appeared. Treating that as delete + add would
re-extract, re-caption (money) and re-embed a file whose content did not
change. This module pairs them up, in two passes:

1. **Same inode** — ``(dev, ino, size, mtime_ns)`` all equal. A rename or a
   move within one filesystem keeps all four, so this is exact and costs
   nothing. Skipped when the inode is unknown (0).
2. **Same content** — ``(size, sha256)``. Catches moves across filesystems
   and copy-then-delete. Hashing is the expensive part, so a new file is hashed
   only when some unmatched missing row has the same size *and* a stored
   hash to compare with.

Ambiguity is never guessed away: when two missing rows (or two new files)
share a key, none of them is matched. A wrong match would attach one file's
chunks, captions and citations to another; a missed match only costs a
re-index.

New files that are not ``index`` (metadata-only secrets, placeholders) are
never hashed — their content is not ours to read. They still match on inode.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Hashable

from app.documents.discovery.ignore import INDEX
from app.documents.types import FsEntry, ManifestRow

Hasher = Callable[[str], "str | None"]


def _unique_pairs(
    rows_by_key: dict[Hashable, list[int]],
    entries_by_key: dict[Hashable, list[int]],
) -> list[tuple[int, int]]:
    """(row index, entry index) for keys held by exactly one of each."""
    pairs = []
    for key, entry_idx in entries_by_key.items():
        row_idx = rows_by_key.get(key)
        if row_idx and len(row_idx) == 1 and len(entry_idx) == 1:
            pairs.append((row_idx[0], entry_idx[0]))
    return pairs


def match_moves(
    missing: list[ManifestRow],
    new: list[FsEntry],
    *,
    hasher: Hasher,
) -> list[tuple[int, FsEntry]]:
    """Pair missing manifest rows with new files that are the same file.

    Returns ``(ManifestRow.id, FsEntry)`` pairs. Each missing row and each new
    entry appears at most once. ``hasher(abs_path)`` must produce the same kind
    of digest the manifest stores in ``sha256`` (a full sha256 by default); a
    ``None`` from it means "unknown" and never matches. Directories in ``new``
    are ignored.
    """
    files = [i for i, e in enumerate(new) if not e.is_dir]
    matched_rows: set[int] = set()
    matched_entries: set[int] = set()
    out: list[tuple[int, FsEntry]] = []

    # Pass 1: same inode.
    rows_by_ino: dict[Hashable, list[int]] = defaultdict(list)
    for ri, row in enumerate(missing):
        if row.ino:
            rows_by_ino[(row.dev, row.ino, row.size, row.mtime_ns)].append(ri)
    entries_by_ino: dict[Hashable, list[int]] = defaultdict(list)
    for ei in files:
        e = new[ei]
        if e.ino:
            entries_by_ino[(e.dev, e.ino, e.size, e.mtime_ns)].append(ei)
    for ri, ei in _unique_pairs(rows_by_ino, entries_by_ino):
        matched_rows.add(ri)
        matched_entries.add(ei)
        out.append((missing[ri].id, new[ei]))

    # Pass 2: same content, among what is left. Empty files all share one
    # hash, so they can never be told apart and are not tried.
    rows_by_size: dict[int, list[int]] = defaultdict(list)
    for ri, row in enumerate(missing):
        if ri not in matched_rows and row.sha256 and row.size > 0:
            rows_by_size[row.size].append(ri)
    if not rows_by_size:
        return out
    rows_by_content: dict[Hashable, list[int]] = defaultdict(list)
    for size, row_idx in rows_by_size.items():
        for ri in row_idx:
            rows_by_content[(size, missing[ri].sha256)].append(ri)
    entries_by_content: dict[Hashable, list[int]] = defaultdict(list)
    for ei in files:
        e = new[ei]
        if ei in matched_entries or e.size not in rows_by_size:
            continue
        if e.placeholder or e.disposition != INDEX:
            continue
        digest = hasher(e.abs_path)
        if digest:
            entries_by_content[(e.size, digest)].append(ei)
    for ri, ei in _unique_pairs(rows_by_content, entries_by_content):
        out.append((missing[ri].id, new[ei]))
    return out


__all__ = ["Hasher", "match_moves"]
