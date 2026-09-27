"""What a file's index entry actually holds: the content summary.

A file's lifecycle ``status`` says where it is in the pipeline (queued,
indexed, failed…), not what search can read from it. "Indexed" used to cover a
scanned decree whose only stored chunk was its file card — name, path, size —
just as it covered a 200-page native text: the file looked indexed, and
research then found nothing to read in it. The summary here is the one answer
every reader of the index gives to "what content does this file have?": the
file listings, the preview, ``cremind docs inspect``, the search/read tools
and research's coverage all derive it from the same inputs.

**Content state** (``state``):

- ``complete`` — extraction covered the whole file (every page read or
  transcribed, nothing cut at a size limit). It says nothing about how good an
  OCR transcription is — only that every page has one.
- ``partial`` — some text is stored, but part of the file is not: pages still
  waiting for OCR, pages that failed, a size or page limit, a truncated
  transcription.
- ``metadata_only`` — only the file card (name, path, type, dates) is stored:
  a kind whose content is never read, an encrypted file, a scan nobody could
  transcribe yet.
- ``unavailable`` — nothing readable: the file failed, is gone, or is still
  waiting for its first indexing.
- ``unknown`` — an entry written before extraction recorded its coverage (a
  legacy scanned PDF), until it is re-read.

A metadata card never counts as readable text: ``readable`` counts body,
OCR and image-description passages only. Text stays previewable while its
vectors are pending (``embedding`` is reported on its own).

**Coverage record.** Extraction writes ``doc_meta["extraction"]`` (see
:func:`coverage_record`): the page inventory of a PDF — native-text pages,
scanned pages and what became of each (transcribed, confirmed blank, waiting
and why, failed) — and any limit that stopped it. Chunk counts are never
stored there: they are derived from the chunks themselves.

Pure functions over rows the caller read; :func:`chunk_stats` and
:func:`preview_page` are the only ones that touch the index.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.documents import types as t

# ── content states and segment types ───────────────────────────────────────

STATE_COMPLETE = "complete"
STATE_PARTIAL = "partial"
STATE_METADATA_ONLY = "metadata_only"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"
STATES = (STATE_COMPLETE, STATE_PARTIAL, STATE_METADATA_ONLY, STATE_UNAVAILABLE, STATE_UNKNOWN)

SEG_TEXT = "text"
SEG_OCR = "ocr"
SEG_IMAGE = "image_description"
SEG_METADATA = "metadata"
SEGMENT_TYPES = (SEG_TEXT, SEG_OCR, SEG_IMAGE, SEG_METADATA)
_SEGMENT_OF_CTYPE = {
    t.CTYPE_BODY: SEG_TEXT,
    t.CTYPE_OCR: SEG_OCR,
    t.CTYPE_CAPTION: SEG_IMAGE,
    t.CTYPE_FILE_CARD: SEG_METADATA,
    t.CTYPE_FOLDER_CARD: SEG_METADATA,
}
READABLE_SEGMENTS = (SEG_TEXT, SEG_OCR, SEG_IMAGE)

# Where the file is in the pipeline (``phase``).
PHASE_QUEUED = "queued"
PHASE_INDEXING = "indexing"
PHASE_INDEXED = "indexed"
PHASE_FAILED = "failed"
PHASE_GONE = "gone"

# What the file tree and the CLI show (``badge``), one per file.
BADGE_WAITING = "waiting"
BADGE_INDEXING = "indexing"
BADGE_INDEXED = "indexed"
BADGE_PARTIAL = "partial"
BADGE_METADATA = "metadata_only"
BADGE_BLOCKED = "blocked"
BADGE_FAILED = "failed"
BADGE_UNKNOWN = "unknown"
BADGE_UNAVAILABLE = "unavailable"

# The coverage record's own version (``doc_meta["extraction"]["v"]``).
COVERAGE_VERSION = 1

# OCR page outcomes, as the coverage record lists them.
OCR_DONE = "done"
OCR_BLANK = "blank"
OCR_TRUNCATED = "truncated"
OCR_FAILED = "failed"
OCR_PENDING = "pending"

# Why pages are waiting (``ocr.reason``), in the order a fix should be tried.
OCR_WAIT_REASONS = ("renderer_missing", "awaiting_vision", "awaiting_consent", "over_cap", "next_batch", "failed")

# Reasons the user can fix in settings (a model to choose, consent to give,
# a component to install) — as opposed to content that is never read.
_BLOCKING = frozenset({"awaiting_vision", "awaiting_consent", "over_cap", "renderer_missing",
                       "awaiting_extractor", "deferred", "next_batch"})

# Preview pages: segments per page by default and at most, and a character
# budget per page (an oversized segment continues on the next page).
PREVIEW_DEFAULT_SEGMENTS = 30
PREVIEW_MAX_SEGMENTS = 60
PREVIEW_MAX_CHARS = 120_000

_CARD_CTYPES = frozenset({t.CTYPE_FILE_CARD, t.CTYPE_FOLDER_CARD})


def segment_type(ctype: str | None) -> str:
    return _SEGMENT_OF_CTYPE.get(ctype or t.CTYPE_BODY, SEG_TEXT)


# ── the coverage record (written by extraction) ────────────────────────────


def coverage_record(
    *,
    pages: int | None = None,
    read_pages: int | None = None,
    text_pages: Iterable[int] = (),
    scanned: Iterable[int] = (),
    ocr: dict[int, str] | None = None,
    ocr_reason: str | None = None,
    unreadable: Iterable[int] = (),
    renderer_missing: bool = False,
    limit: str | None = None,
) -> dict[str, Any]:
    """``doc_meta["extraction"]``: what extraction covered.

    ``ocr`` maps each scanned page to its outcome (``done``, ``blank``,
    ``truncated``, ``failed``, ``pending``); ``ocr_reason`` is why the pending
    ones wait. ``limit`` names a size or page limit that stopped reading
    (``too_large``, ``max_pages``). Page lists are sorted, so the record is a
    pure function of what happened (it sits in the row, not in any hash)."""
    ocr = dict(ocr or {})
    scanned_set = sorted({int(p) for p in scanned} | set(ocr))
    by_state: dict[str, list[int]] = {}
    for page in scanned_set:
        by_state.setdefault(ocr.get(page, OCR_PENDING), []).append(page)
    rec: dict[str, Any] = {"v": COVERAGE_VERSION}
    if pages is not None:
        rec["pages"] = int(pages)
    if read_pages is not None:
        rec["read_pages"] = int(read_pages)
    text_list = sorted({int(p) for p in text_pages})
    if text_list:
        rec["text_pages"] = text_list
    if scanned_set:
        rec["scanned"] = scanned_set
        rec["ocr"] = {k: v for k, v in sorted(by_state.items())}
        if by_state.get(OCR_PENDING):
            rec["ocr_reason"] = ocr_reason or "next_batch"
    unread = sorted({int(p) for p in unreadable})
    if unread:
        rec["unreadable"] = unread
    if renderer_missing:
        rec["renderer_missing"] = True
    if limit:
        rec["limit"] = limit
    return rec


def ocr_unfinished(doc_meta: dict[str, Any] | None) -> bool:
    """Scanned pages of this file still wait for (or failed) OCR — a file in
    this state must not be taken as up to date just because its bytes did not
    change, nor copied to a duplicate as if it were complete."""
    meta = doc_meta or {}
    rec = meta.get("extraction")
    if isinstance(rec, dict):
        ocr = rec.get("ocr") or {}
        return bool(ocr.get(OCR_PENDING) or ocr.get(OCR_FAILED))
    return bool(meta.get("ocr_pending_pages"))


# ── chunk statistics (from the index) ──────────────────────────────────────


@dataclass
class ChunkStats:
    """One file's stored chunks, by segment type."""

    counts: dict[str, int] = field(default_factory=dict)
    chars: dict[str, int] = field(default_factory=dict)
    total: int = 0
    # Chunks whose vector is in the active collection.
    vectors: int = 0

    @property
    def readable(self) -> int:
        return sum(self.counts.get(k, 0) for k in READABLE_SEGMENTS)

    @property
    def readable_chars(self) -> int:
        return sum(self.chars.get(k, 0) for k in READABLE_SEGMENTS)


def chunk_stats(db: Any, file_ids: Iterable[int], *, gen: int | None = None) -> dict[int, ChunkStats]:
    """Per-file chunk counts, characters and vector coverage, in one grouped
    query per 500 files. ``gen`` is the active collection's generation (None:
    no vectors are usable, so none count as ready)."""
    ids = sorted({int(i) for i in file_ids})
    out: dict[int, ChunkStats] = {i: ChunkStats() for i in ids}
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        rows = db.read_sql(
            "SELECT file_id, ctype, COUNT(*) AS n, COALESCE(SUM(length(text)), 0) AS chars, "
            "COALESCE(SUM(CASE WHEN vec_gen = ? THEN 1 ELSE 0 END), 0) AS vec "
            f"FROM chunks WHERE file_id IN ({','.join('?' * len(batch))}) GROUP BY file_id, ctype",
            [-1 if gen is None else int(gen), *batch],
        )
        for r in rows:
            st = out.setdefault(int(r["file_id"]), ChunkStats())
            seg = segment_type(r.get("ctype"))
            st.counts[seg] = st.counts.get(seg, 0) + int(r["n"])
            st.chars[seg] = st.chars.get(seg, 0) + int(r["chars"])
            st.total += int(r["n"])
            st.vectors += int(r["vec"])
    return out


def active_gen(db: Any) -> int | None:
    try:
        col = db.active_collection()
    except Exception:  # noqa: BLE001 — a summary must render without vectors
        return None
    return int(col["gen"]) if col and col.get("gen") is not None else None


def body_revision(rows: Iterable[dict[str, Any]]) -> str:
    """The content revision of a file: a hash of its readable passages in
    order (cards left out — a card changes on a rename, the text does not).
    Equal revisions mean the same passages in the same order."""
    h = hashlib.blake2b(digest_size=8)
    for r in rows:
        if r.get("ctype") in _CARD_CTYPES:
            continue
        h.update(f"{r.get('ctype')}:{r.get('text_hash')}:{int(r.get('occ') or 0)}\n".encode("utf-8"))
    return h.hexdigest()


def file_revision(db: Any, file_id: int) -> str:
    rows = db.read_sql(
        "SELECT ctype, text_hash, occ FROM chunks WHERE file_id = ? ORDER BY ordinal, id", (int(file_id),))
    return body_revision(rows)


# ── reasons ────────────────────────────────────────────────────────────────

_METADATA_REASON_TEXT = {
    "encrypted": "The file is password-protected, so its text cannot be read.",
    "placeholder": "The file is a cloud placeholder that is not downloaded to this computer.",
    "secret": "The file looks like a credential or key file, so only its details are indexed.",
    "legacy_format": "This file format is too old to read.",
    "too_large": "The file is larger than the indexing limit, so only its details are indexed.",
    "not_downloadable": "Google Drive does not allow this file to be downloaded.",
}

_CAPTION_WAIT = {
    "awaiting_vision": ("No Specialized Vision Model is chosen, so this image has no description yet.",
                        "vision_model"),
    "awaiting_consent": ("Sending images to the vision model has not been allowed yet, so this image has no "
                         "description.", "vision_consent"),
    "over_cap": ("Today's vision quota is used up; this image is described when it resets.", None),
    "captions_off": ("Image descriptions are turned off.", "vision_consent"),
    "skipped_small": ("The image is too small (or looks like an icon) to describe.", None),
    "failed": ("The vision model failed to describe this image; it is retried.", "retry"),
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _ocr_wait_text(reason: str, n: int) -> tuple[str, str | None]:
    pages = _plural(n, "page")
    return {
        "renderer_missing": (f"{pages} could not be prepared for OCR: the PDF page renderer (pypdfium2) is not "
                             "installed.", "install"),
        "awaiting_vision": (f"{pages} {'is' if n == 1 else 'are'} waiting for OCR: no Specialized Vision Model is "
                            "chosen (Settings → LLM Providers).", "vision_model"),
        "awaiting_consent": (f"{pages} {'is' if n == 1 else 'are'} waiting for OCR: sending pages to the vision "
                             "model has not been allowed yet (Settings → My Documents).", "vision_consent"),
        "over_cap": (f"{pages} {'is' if n == 1 else 'are'} waiting for OCR: today's vision quota is used up; "
                     "transcription continues when it resets.", None),
        "next_batch": (f"{pages} {'is' if n == 1 else 'are'} queued for OCR in a later batch.", None),
        "failed": (f"{pages} could not be transcribed; {'it is' if n == 1 else 'they are'} retried.", "retry"),
    }.get(reason, (f"{pages} {'is' if n == 1 else 'are'} waiting for OCR.", None))


def _reason(code: str, message: str, action: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message": message}
    if action:
        out["action"] = action
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


# ── the summary ────────────────────────────────────────────────────────────


def _pages_view(meta: dict[str, Any], stats: ChunkStats) -> dict[str, Any] | None:
    """A PDF's page coverage from its coverage record (None when it has
    none): total pages, pages with native text, pages transcribed, confirmed
    blank, waiting, failed."""
    rec = meta.get("extraction")
    if not isinstance(rec, dict):
        return None
    total = rec.get("pages")
    ocr = rec.get("ocr") or {}
    scanned = rec.get("scanned") or []
    if total is None and not scanned and not rec.get("text_pages"):
        return None
    return {
        "total": total,
        "read": rec.get("read_pages"),
        "text": len(rec.get("text_pages") or []),
        "scanned": len(scanned),
        "ocr_done": len(ocr.get(OCR_DONE) or []),
        "blank": len(ocr.get(OCR_BLANK) or []),
        "truncated": len(ocr.get(OCR_TRUNCATED) or []),
        "pending": len(ocr.get(OCR_PENDING) or []),
        "failed": len(ocr.get(OCR_FAILED) or []),
        "unreadable": len(rec.get("unreadable") or []),
    }


def summarize(
    row: dict[str, Any],
    stats: ChunkStats | None,
    *,
    in_flight: bool = False,
    gen: int | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """The content summary of one file row (see the module docstring).

    ``stats`` are its chunks (:func:`chunk_stats`); ``in_flight`` whether a
    pipeline worker holds it right now; ``gen`` the active vector collection
    (None: no vectors ready); ``revision`` its content revision when known."""
    stats = stats or ChunkStats()
    status = str(row.get("status") or "")
    reason = str(row.get("status_reason") or "")
    meta = row.get("doc_meta") if isinstance(row.get("doc_meta"), dict) else {}
    kind = row.get("kind") or ""
    readable = stats.readable
    reasons: list[dict[str, Any]] = []

    if status in ("tombstone", "missing"):
        phase = PHASE_GONE
    elif in_flight:
        phase = PHASE_INDEXING
    elif status in ("dirty", "deferred"):
        phase = PHASE_QUEUED
    elif status in ("error", "awaiting_extractor"):
        phase = PHASE_FAILED
    else:
        phase = PHASE_INDEXED

    pages = _pages_view(meta, stats) if kind == t.KIND_PDF else None
    rec = meta.get("extraction") if isinstance(meta.get("extraction"), dict) else None
    state: str

    if phase == PHASE_GONE:
        state = STATE_UNAVAILABLE
        reasons.append(_reason("gone", "The file is no longer in the indexed folder." if status == "tombstone"
                               else "The file disappeared from the folder; its entry is kept for now."))
    elif status == "metadata_only":
        state = STATE_PARTIAL if readable else STATE_METADATA_ONLY
        text = _METADATA_REASON_TEXT.get(reason)
        if text is None:
            text = ("Only the file's details are indexed for this type of file." if reason in t.METADATA_ONLY_KINDS
                    or reason == kind else "Only the file's details are indexed.")
        reasons.append(_reason(reason or "metadata_only", text))
    elif phase == PHASE_FAILED:
        state = STATE_PARTIAL if readable else STATE_UNAVAILABLE
        if status == "awaiting_extractor":
            reasons.append(_reason("awaiting_extractor",
                                   f"A reader for this format is not installed ({reason or 'extractor'}).",
                                   "install"))
        else:
            err = str(row.get("error") or "").strip()
            msg = "Extraction failed" + (f" ({reason})" if reason else "") + (f": {err[:300]}" if err else ".")
            reasons.append(_reason("extraction_failed", msg, "retry"))
        if readable:
            reasons.append(_reason("stale", "The text shown is from an earlier successful indexing."))
    elif phase in (PHASE_QUEUED, PHASE_INDEXING) and not readable and not stats.total:
        state = STATE_UNAVAILABLE
        if status == "deferred":
            reasons.append(_reason("deferred", "Waiting for storage space before it can be indexed.", "wait"))
        else:
            reasons.append(_reason("queued" if phase == PHASE_QUEUED else "indexing",
                                   "Waiting to be indexed." if phase == PHASE_QUEUED else "Being indexed now."))
    else:
        state, content_reasons = _indexed_state(row, meta, rec, stats, kind, reason)
        reasons += content_reasons
        if phase in (PHASE_QUEUED, PHASE_INDEXING):
            reasons.append(_reason("refreshing", "An update of this file is being indexed; the stored text is "
                                   "shown until it finishes." if phase == PHASE_INDEXING else
                                   "An update of this file is queued; the stored text is shown until then."))

    embedding = _embedding_view(stats, gen)
    summary: dict[str, Any] = {
        "state": state,
        "phase": phase,
        "readable": {"passages": readable, "chars": stats.readable_chars},
        "segments": {k: int(stats.counts.get(k, 0)) for k in SEGMENT_TYPES},
        "chars": {k: int(stats.chars.get(k, 0)) for k in SEGMENT_TYPES},
        "indexed_at": float(row["indexed_at"]) * 1000 if row.get("indexed_at") else None,
        "pages": pages,
        "reasons": reasons,
        "embedding": embedding,
        "refresh_queued": phase in (PHASE_QUEUED, PHASE_INDEXING) and bool(stats.total),
        "previewable": stats.total > 0,
    }
    if revision is not None:
        summary["revision"] = revision
    summary["badge"] = badge(summary)
    summary["headline"] = headline(summary)
    return summary


def _indexed_state(
    row: dict[str, Any], meta: dict[str, Any], rec: dict[str, Any] | None, stats: ChunkStats, kind: str,
    reason: str,
) -> tuple[str, list[dict[str, Any]]]:
    """The content state of a file whose extraction finished (or that has
    stored content while an update is queued)."""
    readable = stats.readable
    reasons: list[dict[str, Any]] = []
    incomplete = False

    if kind == t.KIND_IMAGE:
        cap = row.get("caption_state")
        if stats.counts.get(SEG_IMAGE):
            return STATE_COMPLETE, reasons
        text, action = _CAPTION_WAIT.get(str(cap or ""), ("This image has no description yet.", None))
        reasons.append(_reason(str(cap or "no_description"), text, action))
        return STATE_METADATA_ONLY, reasons

    if rec is not None:
        ocr = rec.get("ocr") or {}
        pending = ocr.get(OCR_PENDING) or []
        failed = ocr.get(OCR_FAILED) or []
        truncated = ocr.get(OCR_TRUNCATED) or []
        if pending:
            why = str(rec.get("ocr_reason") or "next_batch")
            if rec.get("renderer_missing"):
                why = "renderer_missing"
            text, action = _ocr_wait_text(why, len(pending))
            reasons.append(_reason(why, text, action, pages=len(pending)))
            incomplete = True
        if failed:
            text, action = _ocr_wait_text("failed", len(failed))
            reasons.append(_reason("ocr_failed", text, action, pages=len(failed)))
            incomplete = True
        if truncated:
            reasons.append(_reason("ocr_truncated", f"The transcription of {_plural(len(truncated), 'page')} was "
                                   "cut off by the model's output limit.", "retry", pages=len(truncated)))
            incomplete = True
        unreadable = rec.get("unreadable") or []
        if unreadable:
            reasons.append(_reason("unreadable_pages", f"{_plural(len(unreadable), 'page')} of the PDF "
                                   f"{'is' if len(unreadable) == 1 else 'are'} damaged and could not be read.",
                                   pages=len(unreadable)))
            incomplete = True
        limit = rec.get("limit")
        if limit:
            reasons.append(_reason(str(limit), "Only the first part of the file was read (a size or page limit)."))
            incomplete = True
    else:
        # Written before extraction recorded its coverage.
        legacy_pending = meta.get("ocr_pending_pages") or []
        if legacy_pending:
            why = str(row.get("caption_state") or "next_batch")
            text, action = _ocr_wait_text(why if why in OCR_WAIT_REASONS else "next_batch", len(legacy_pending))
            reasons.append(_reason(why, text, action, pages=len(legacy_pending)))
            incomplete = True
        elif kind == t.KIND_PDF and meta.get("scanned_pages"):
            reasons.append(_reason("legacy", "This scanned PDF was indexed before page coverage was recorded; "
                                   "it is re-read to check every page."))
            return STATE_UNKNOWN, reasons
        if reason.startswith("partial"):
            detail = reason.split(":", 1)[1] if ":" in reason else ""
            if detail == "corrupt":
                reasons.append(_reason("unreadable_pages", "Some pages of the file are damaged and could not be "
                                       "read."))
            else:
                reasons.append(_reason(detail or "too_large", "Only the first part of the file was read (a size or "
                                       "page limit)."))
            incomplete = True

    if rec is not None and reason.startswith("partial") and not incomplete:
        detail = reason.split(":", 1)[1] if ":" in reason else "too_large"
        reasons.append(_reason(detail, "Only the first part of the file was read (a size or page limit)."))
        incomplete = True

    if not readable:
        if not reasons:
            reasons.append(_reason("no_text", "No text was found in the file."))
        return STATE_METADATA_ONLY, reasons
    return (STATE_PARTIAL if incomplete else STATE_COMPLETE), reasons


def _embedding_view(stats: ChunkStats, gen: int | None) -> dict[str, Any]:
    total = stats.total
    if gen is None:
        return {"state": "unavailable", "ready": 0, "total": total}
    ready = min(stats.vectors, total)
    state = "ready" if total and ready >= total else ("pending" if not ready else "partial")
    if not total:
        state = "ready"
    return {"state": state, "ready": ready, "total": total}


def badge(summary: dict[str, Any]) -> str:
    """One word for the file tree and the CLI: see the BADGE_* constants."""
    phase, state = summary.get("phase"), summary.get("state")
    codes = {r.get("code") for r in summary.get("reasons") or []}
    readable = int((summary.get("readable") or {}).get("passages") or 0)
    if phase == PHASE_GONE:
        return BADGE_UNAVAILABLE
    if phase == PHASE_FAILED:
        return BADGE_FAILED
    if phase == PHASE_INDEXING and not readable:
        return BADGE_INDEXING
    if phase == PHASE_QUEUED and not readable and not summary.get("previewable"):
        return BADGE_WAITING
    if state == STATE_COMPLETE:
        return BADGE_INDEXED
    if state == STATE_PARTIAL:
        return BADGE_PARTIAL
    if state == STATE_UNKNOWN:
        return BADGE_UNKNOWN
    if state == STATE_METADATA_ONLY:
        return BADGE_BLOCKED if codes & _BLOCKING else BADGE_METADATA
    if phase == PHASE_QUEUED:
        return BADGE_WAITING
    if phase == PHASE_INDEXING:
        return BADGE_INDEXING
    return BADGE_FAILED


def headline(summary: dict[str, Any]) -> str:
    """One sentence saying what the index holds for the file, e.g. "Only
    file details were indexed. 8 pages are waiting for OCR." """
    state = summary.get("state")
    readable = summary.get("readable") or {}
    passages = int(readable.get("passages") or 0)
    pages = summary.get("pages") or {}
    first = next((r.get("message") for r in summary.get("reasons") or [] if r.get("code") not in ("refreshing",)),
                 None)
    if state == STATE_COMPLETE:
        head = f"Indexed: {_plural(passages, 'passage')} of text"
        if pages and pages.get("total"):
            head += f" from {_plural(int(pages['total']), 'page')}"
            if pages.get("ocr_done"):
                head += f" ({pages['ocr_done']} transcribed by OCR)"
        return head + "."
    if state == STATE_PARTIAL:
        head = f"Partly indexed: {_plural(passages, 'passage')} of text are stored."
        return f"{head} {first}" if first else head
    if state == STATE_METADATA_ONLY:
        head = "Only file details were indexed."
        return f"{head} {first}" if first else head
    if state == STATE_UNKNOWN:
        return first or "The index does not record how much of this file was read."
    return first or "No content is indexed for this file."


def content_note(row: dict[str, Any]) -> str | None:
    """A short note, from the row alone, when a file's stored content is
    known to be incomplete — what search, find and read print next to it so
    the agent never takes a scan's file card for the document: "text not
    indexed yet: 8 of 8 scanned pages are waiting for OCR (awaiting
    consent)". None when nothing is known to be missing."""
    if row.get("kind") != t.KIND_PDF:
        return None
    meta = row.get("doc_meta") if isinstance(row.get("doc_meta"), dict) else {}
    rec = meta.get("extraction") if isinstance(meta.get("extraction"), dict) else None
    if rec is None:
        pending = meta.get("ocr_pending_pages") or []
        if pending:
            return f"{_plural(len(pending), 'scanned page')} not transcribed yet ({row.get('caption_state') or 'waiting'})"
        if meta.get("scanned_pages"):
            return "scanned pages: coverage unknown until the file is re-read"
        return None
    ocr = rec.get("ocr") or {}
    scanned = len(rec.get("scanned") or [])
    missing = len(ocr.get(OCR_PENDING) or []) + len(ocr.get(OCR_FAILED) or [])
    if not missing:
        return None
    have_text = bool(rec.get("text_pages") or ocr.get(OCR_DONE) or ocr.get(OCR_TRUNCATED))
    why = str(rec.get("ocr_reason") or ("failed" if ocr.get(OCR_FAILED) else "waiting")).replace("_", " ")
    head = "partly indexed" if have_text else "text not indexed yet"
    return f"{head}: {missing} of {_plural(scanned, 'scanned page')} not transcribed ({why})"


def unread_code(summary: dict[str, Any]) -> str | None:
    """Why research cannot read this file's content (None: it can, fully or
    partly) — the reason codes research's coverage table reports."""
    state = summary.get("state")
    phase = summary.get("phase")
    codes = [r.get("code") for r in summary.get("reasons") or []]
    if state in (STATE_COMPLETE, STATE_PARTIAL, STATE_UNKNOWN):
        return None
    if phase in (PHASE_QUEUED, PHASE_INDEXING):
        return "not_indexed_yet"
    if phase == PHASE_GONE:
        return "gone"
    for code in codes:
        if code in ("awaiting_vision", "awaiting_consent", "over_cap", "renderer_missing", "next_batch"):
            return "awaiting_ocr" if code != "over_cap" or summary.get("pages") else code
    return codes[0] if codes else "metadata_only"


# ── the preview (read from the index; nothing is extracted or embedded) ────


class StalePreview(Exception):
    """A preview cursor from an earlier revision of the file."""

    def __init__(self, revision: str) -> None:
        super().__init__("The file was re-indexed since this page was loaded; reload the preview.")
        self.revision = revision


class BadCursor(ValueError):
    pass


def _encode_cursor(revision: str, index: int, offset: int) -> str:
    raw = json.dumps({"r": revision, "i": int(index), "o": int(offset)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int, int]:
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode((cursor + pad).encode("ascii")).decode("ascii"))
        rev, index, offset = str(data["r"]), int(data["i"]), int(data.get("o") or 0)
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as exc:
        raise BadCursor("cursor is not one this server handed out") from exc
    if index < 0 or offset < 0:
        raise BadCursor("cursor is not one this server handed out")
    return rev, index, offset


# doc_meta keys shown as the file's metadata (the coverage record is the
# summary's ``pages``; internal bookkeeping stays out).
_META_KEYS = ("title", "author", "last_modified_by", "subject", "keywords", "created", "modified", "pages",
              "sheets", "slides", "words", "app", "encoding", "legal")


def preview_page(
    db: Any,
    row: dict[str, Any],
    *,
    cursor: str | None = None,
    limit: int = PREVIEW_DEFAULT_SEGMENTS,
    max_chars: int = PREVIEW_MAX_CHARS,
    in_flight: bool = False,
) -> dict[str, Any]:
    """One page of a file's stored content, in source order.

    Every stored passage appears exactly once across the pages — duplicates
    (two identical paragraphs are two passages) included — and a passage
    longer than a page's character budget continues on the next page from
    where this one stopped. The cursor is bound to the file's content
    revision: a file re-indexed between two pages raises
    :class:`StalePreview`, never a page stitched from two versions."""
    from app.documents.cite import locator_label, make_token

    limit = max(1, min(int(limit or PREVIEW_DEFAULT_SEGMENTS), PREVIEW_MAX_SEGMENTS))
    max_chars = max(1000, min(int(max_chars or PREVIEW_MAX_CHARS), PREVIEW_MAX_CHARS))
    fid = str(row.get("cite_id") or "")
    rows = db.read_sql(
        "SELECT id, ctype, ordinal, occ, text_hash, heading, text, locator, vec_gen FROM chunks "
        "WHERE file_id = ? ORDER BY ordinal, id",
        (int(row["id"]),), table="chunks",
    )
    cards = [r for r in rows if r.get("ctype") in _CARD_CTYPES]
    body = [r for r in rows if r.get("ctype") not in _CARD_CTYPES]
    revision = body_revision(body)
    index, offset = 0, 0
    if cursor:
        rev, index, offset = _decode_cursor(cursor)
        if rev != revision:
            raise StalePreview(revision)
        if index > len(body) or (index < len(body) and offset > len(body[index].get("text") or "")):
            raise BadCursor("cursor points past the end of the file")

    segments: list[dict[str, Any]] = []
    used = 0
    i, off = index, offset
    while i < len(body) and len(segments) < limit:
        r = body[i]
        text = r.get("text") or ""
        rest = text[off:]
        room = max_chars - used
        if len(rest) > room:
            if segments:
                break  # starts on the next page, whole if it fits there
            piece, end = rest[:room], off + room
        else:
            piece, end = rest, len(text)
        loc = r.get("locator") if isinstance(r.get("locator"), dict) else {}
        seg: dict[str, Any] = {
            "token": make_token(fid, r.get("text_hash")),
            "index": i,
            "ordinal": int(r.get("ordinal") or 0),
            "type": segment_type(r.get("ctype")),
            "heading": r.get("heading") or "",
            "locator": loc,
            "locator_label": locator_label(loc),
            "text": piece,
        }
        if off or end < len(text):
            seg["part"] = {"start": off, "end": end, "length": len(text)}
        segments.append(seg)
        used += len(piece)
        if end < len(text):
            i, off = i, end
            break
        i, off = i + 1, 0
    next_cursor = _encode_cursor(revision, i, off) if i < len(body) else None

    stats = ChunkStats()
    gen = active_gen(db)
    for r in rows:
        seg = segment_type(r.get("ctype"))
        stats.counts[seg] = stats.counts.get(seg, 0) + 1
        stats.chars[seg] = stats.chars.get(seg, 0) + len(r.get("text") or "")
        stats.total += 1
        if gen is not None and r.get("vec_gen") == gen:
            stats.vectors += 1
    meta = row.get("doc_meta") if isinstance(row.get("doc_meta"), dict) else {}
    return {
        "fid": fid,
        "name": row.get("name") or str(row.get("rel_path") or "").rsplit("/", 1)[-1],
        "rel_path": row.get("rel_path"),
        "kind": row.get("kind"),
        "source": row.get("source"),
        "status": row.get("status"),
        "summary": summarize(row, stats, in_flight=in_flight, gen=gen, revision=revision),
        "metadata": {
            "card": "\n\n".join(c.get("text") or "" for c in cards) or None,
            "document": {k: meta[k] for k in _META_KEYS if meta.get(k) not in (None, "", [], {})},
        },
        "revision": revision,
        "total_segments": len(body),
        "start_index": index,
        "segments": segments,
        "next_cursor": next_cursor,
    }


__all__ = [
    "BADGE_BLOCKED", "BADGE_FAILED", "BADGE_INDEXED", "BADGE_INDEXING", "BADGE_METADATA", "BADGE_PARTIAL",
    "BADGE_UNAVAILABLE", "BADGE_UNKNOWN", "BADGE_WAITING", "BadCursor", "COVERAGE_VERSION", "ChunkStats",
    "OCR_BLANK", "OCR_DONE", "OCR_FAILED", "OCR_PENDING", "OCR_TRUNCATED", "PREVIEW_DEFAULT_SEGMENTS",
    "PREVIEW_MAX_CHARS", "PREVIEW_MAX_SEGMENTS", "SEGMENT_TYPES", "STATES", "STATE_COMPLETE",
    "STATE_METADATA_ONLY", "STATE_PARTIAL", "STATE_UNAVAILABLE", "STATE_UNKNOWN", "StalePreview", "active_gen",
    "badge", "body_revision", "chunk_stats", "coverage_record", "file_revision", "headline", "ocr_unfinished",
    "preview_page", "segment_type", "summarize", "unread_code",
]
