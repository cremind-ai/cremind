"""The keyword side of search: FTS5 bm25 over the per-profile index.

Two lists come out of one query — the word list (every token OR-ed) and the
phrase list (identifiers, legal references and quoted text as exact phrases)
— because fusion weighs an exact "45/2013/QH13" far above the loose words
45, 2013 and qh13 appearing somewhere in a chunk.

Restricting to the scope: for a small scope (≤ 2000 files) the FTS query is
restricted to those files' chunks in SQL; for a large or unrestricted scope it
runs over the whole index with a larger limit and the engine drops
out-of-scope hits afterwards, which is far cheaper than binding a hundred
thousand ids. Everything here is synchronous; the engine runs it in a worker
thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.documents.index.schema import LEXICAL_FTS5
from app.documents.query.filters import Scope
from app.documents.query.terms import QueryTerms, build_match, build_phrase_match, like_terms
from app.utils.logger import logger

# Above this many scope files, restricting in SQL costs more than filtering
# the hits afterwards (one statement per 500 ids).
PREFILTER_MAX_FILES = 2000
# Unrestricted queries over-fetch by this factor (capped) so enough hits
# survive the scope filter.
OVERFETCH = 5
OVERFETCH_CAP = 1000


@dataclass
class LexicalResult:
    main: list[int] = field(default_factory=list)      # chunk ids, best first
    phrase: list[int] = field(default_factory=list)
    available: bool = True
    reason: str | None = None
    # The index has no FTS5 and the LIKE fallback produced ``main``.
    like: bool = False


def _run(db: Any, match: str, *, scope: Scope, limit: int, ctype: str | None) -> list[int]:
    if ctype is not None:
        hits = db.fts_search_ctype(match, ctype, limit=min(OVERFETCH_CAP, limit * OVERFETCH))
        return [cid for cid, _ in hits]
    ids = scope.file_ids
    if ids is not None and len(ids) <= PREFILTER_MAX_FILES:
        hits = db.fts_search(match, limit=limit, file_ids=ids) if ids else []
        out = [cid for cid, _ in hits]
        if scope.cards_allowed and scope.folder_prefixes:
            # Folder cards carry no file id, so the file-restricted query can
            # never return one; ask for them separately.
            out += [cid for cid, _ in db.fts_search_ctype(match, "folder_card", limit=20)]
        return out
    hits = db.fts_search(match, limit=min(OVERFETCH_CAP, limit * OVERFETCH))
    return [cid for cid, _ in hits]


def search_lexical(
    db: Any, terms: QueryTerms, *, scope: Scope, limit: int, ctype: str | None = None,
) -> LexicalResult:
    """Word and phrase hits for ``terms`` within ``scope`` (``ctype`` limits
    to one chunk type, e.g. file cards for the catalog). Never raises: an
    index without FTS5, or a query FTS5 rejects, comes back as
    ``available=False`` with the reason."""
    if db.lexical != LEXICAL_FTS5:
        return LexicalResult(available=False, reason="this index has no full-text table (SQLite without FTS5)")
    res = LexicalResult()
    try:
        match = build_match(terms)
        if match:
            res.main = _run(db, match, scope=scope, limit=limit, ctype=ctype)
        phrase = build_phrase_match(terms)
        if phrase:
            res.phrase = _run(db, phrase, scope=scope, limit=limit, ctype=ctype)
    except (ValueError, RuntimeError) as exc:
        # Every term is quoted, so a rejection means a bug or a damaged index
        # — worth a log line, not an error the agent has to interpret.
        logger.warning(f"[documents] keyword search failed: {exc}")
        return LexicalResult(available=False, reason=f"keyword search failed ({exc})")
    return res


def search_like(db: Any, terms: QueryTerms, *, scope: Scope, limit: int) -> LexicalResult:
    """The degraded keyword path for an index without FTS5: a substring scan
    of the chunks, ranked by how many terms matched. Slow on a big index, so
    the engine only uses it when vector search is unavailable too."""
    words = like_terms(terms)
    if not words:
        return LexicalResult(like=True)
    ids = scope.file_ids
    try:
        if ids is not None and len(ids) <= PREFILTER_MAX_FILES:
            hits = db.like_search(words, limit=limit, file_ids=ids) if ids else []
        else:
            hits = db.like_search(words, limit=min(OVERFETCH_CAP, limit * OVERFETCH))
    except Exception as exc:  # noqa: BLE001 — degraded path: report, never raise
        logger.warning(f"[documents] keyword fallback failed: {exc}")
        return LexicalResult(available=False, reason=f"keyword search failed ({exc})", like=True)
    return LexicalResult(main=[cid for cid, _ in hits], like=True)


__all__ = ["LexicalResult", "OVERFETCH", "OVERFETCH_CAP", "PREFILTER_MAX_FILES", "search_lexical", "search_like"]
