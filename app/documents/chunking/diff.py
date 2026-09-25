"""The chunk diff: which of a file's chunks need a new embedding.

Chunk identity is ``text_hash`` — the hash of exactly what gets embedded —
not position. After a re-chunk, a chunk whose text already exists in the
file keeps its row and its vector, whatever its new ordinal or locator; only
text the file has never had before is embedded. Position-based matching
(ordinal N old ↔ ordinal N new) would turn one inserted paragraph into a
re-embed of everything after it.

Duplicates are handled as a multiset: old chunks are queued per hash in
ordinal order and consumed in the new order, so three copies of a boilerplate
paragraph that become four cost one embedding, and deleting one copy costs
none. ``occ`` is renumbered over the new list so the ``n``-th copy of a text
always has ``occ = n - 1``.

File-local and DB-free on purpose: the write path turns the result into one
transaction (update kept rows' ordinal/locator/occ, insert added, delete
removed) and may still reuse a vector for an "added" hash that exists in
another file of the profile.
"""

from __future__ import annotations

from collections import deque

from app.documents.types import Chunk, ChunkDiff, OldChunk


def diff_chunks(old: list[OldChunk], new: list[Chunk]) -> ChunkDiff:
    """Match ``new`` chunks against a file's ``old`` ones by ``text_hash``.

    - matched → ``keep`` as ``(old id, new chunk)`` (same text; ordinal,
      locator, occ may differ — a DB-only update, no embedding);
    - unmatched new → ``add`` (needs a vector);
    - old left over → ``remove`` (ids, in old ordinal order).

    Sets ``occ`` on the ``new`` chunks in place (0-based count of earlier
    chunks in ``new`` with the same hash); pass the file's complete chunk
    list (cards, body, captions, OCR) so the numbering is file-wide.
    """
    seen: dict[str, int] = {}
    for c in new:
        c.occ = seen.get(c.text_hash, 0)
        seen[c.text_hash] = c.occ + 1

    pool: dict[str, deque[OldChunk]] = {}
    for o in sorted(old, key=lambda o: (o.ordinal, o.occ, o.id)):
        pool.setdefault(o.text_hash, deque()).append(o)

    result = ChunkDiff()
    for c in new:
        q = pool.get(c.text_hash)
        if q:
            result.keep.append((q.popleft().id, c))
        else:
            result.add.append(c)
    leftovers = [o for q in pool.values() for o in q]
    leftovers.sort(key=lambda o: (o.ordinal, o.occ, o.id))
    result.remove = [o.id for o in leftovers]
    return result


__all__ = ["diff_chunks"]
