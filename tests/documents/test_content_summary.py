"""The content summary: what a file's index entry holds, said the same way
everywhere (file listings, the preview, the CLI, research coverage).

Pins, in order of how much damage a regression would do:

- a metadata card never makes a file look readable: a scanned PDF whose only
  chunk is its card is ``metadata_only`` with the pages waiting for OCR, never
  "indexed";
- OCR that waits (no vision model, no consent, the quota, a missing renderer)
  or failed stays visible as ``partial`` with the reason;
- a legacy scanned PDF with no coverage record is ``unknown`` until re-read;
- text stays previewable while its vectors are pending;
- the preview pages through every stored passage exactly once — identical
  passages and passages longer than a page included — and refuses a cursor
  from before a re-index.
"""

from __future__ import annotations

import pytest

from app.documents import content as C
from app.documents import types as t
from app.documents.index import IndexDB
from app.documents.textnorm import text_hash
from app.documents.types import Chunk, ChunkDiff


def _chunk(ordinal: int, text: str, ctype: str = t.CTYPE_BODY, **loc) -> Chunk:
    return Chunk(ordinal=ordinal, ctype=ctype, heading="", text=text, text_hash=text_hash("", text),
                 locator=dict(loc))


def _card(text: str = "File: scan.pdf\nPath: scan.pdf\nType: pdf") -> Chunk:
    return _chunk(-1, text, t.CTYPE_FILE_CARD)


@pytest.fixture
def db(tmp_path):
    d = IndexDB.open(str(tmp_path / "index.db"), profile_uid="u1")
    yield d
    d.close()


def _file(db, rel, chunks, **fields):
    row = db.insert_file("local", rel, f"h-{rel}", name=rel.rsplit("/", 1)[-1], **fields)
    if chunks:
        from app.documents.chunking import diff_chunks

        diff = diff_chunks([], chunks)
        db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=ChunkDiff(add=diff.add))
    return db.get_file(row["id"])


def _summary(db, row, **kw):
    stats = C.chunk_stats(db, [row["id"]], gen=C.active_gen(db))
    return C.summarize(row, stats[row["id"]], gen=C.active_gen(db), **kw)


def test_a_card_alone_is_never_readable(db):
    rec = C.coverage_record(pages=8, read_pages=8, scanned=range(1, 9), ocr={}, ocr_reason="awaiting_consent")
    row = _file(db, "scan.pdf", [_card()], kind="pdf", status="indexed", caption_state="awaiting_consent",
                doc_meta={"extraction": rec})
    s = _summary(db, row)
    assert s["state"] == C.STATE_METADATA_ONLY
    assert s["readable"] == {"passages": 0, "chars": 0}
    assert s["segments"]["metadata"] == 1
    assert s["badge"] == C.BADGE_BLOCKED
    assert s["pages"]["pending"] == 8 and s["pages"]["total"] == 8
    assert s["headline"].startswith("Only file details were indexed. 8 pages are waiting for OCR")
    assert any(r["code"] == "awaiting_consent" and r.get("action") == "vision_consent" for r in s["reasons"])
    assert C.unread_code(s) == "awaiting_ocr"


def test_partly_transcribed_scan_is_partial(db):
    ocr = {1: C.OCR_DONE, 2: C.OCR_DONE, 3: C.OCR_BLANK, 4: C.OCR_FAILED, 5: C.OCR_PENDING}
    rec = C.coverage_record(pages=5, read_pages=5, scanned=range(1, 6), ocr=ocr, ocr_reason="over_cap")
    row = _file(db, "scan.pdf", [_card(), _chunk(0, "Article 1. Scope.", t.CTYPE_OCR, page=1),
                                 _chunk(1, "Article 2. Terms.", t.CTYPE_OCR, page=2)],
                kind="pdf", status="indexed", doc_meta={"extraction": rec})
    s = _summary(db, row)
    assert s["state"] == C.STATE_PARTIAL and s["badge"] == C.BADGE_PARTIAL
    assert s["segments"]["ocr"] == 2 and s["readable"]["passages"] == 2
    assert s["pages"] == {"total": 5, "read": 5, "text": 0, "scanned": 5, "ocr_done": 2, "blank": 1,
                          "truncated": 0, "pending": 1, "failed": 1, "unreadable": 0}
    codes = {r["code"] for r in s["reasons"]}
    assert {"over_cap", "ocr_failed"} <= codes
    assert C.unread_code(s) is None  # partly readable: research reads it and reports the gap


def test_complete_native_text_and_blank_pages(db):
    rec = C.coverage_record(pages=3, read_pages=3, text_pages=[1, 2], scanned=[3], ocr={3: C.OCR_BLANK})
    row = _file(db, "doc.pdf", [_card(), _chunk(0, "Chapter one.", page=1), _chunk(1, "Chapter two.", page=2)],
                kind="pdf", status="indexed", doc_meta={"extraction": rec})
    s = _summary(db, row)
    assert s["state"] == C.STATE_COMPLETE and s["badge"] == C.BADGE_INDEXED
    assert s["pages"]["blank"] == 1 and s["pages"]["pending"] == 0
    assert s["headline"] == "Indexed: 2 passages of text from 3 pages."


def test_legacy_scanned_pdf_is_unknown(db):
    row = _file(db, "old.pdf", [_card()], kind="pdf", status="indexed", doc_meta={"scanned_pages": [1, 2]})
    s = _summary(db, row)
    assert s["state"] == C.STATE_UNKNOWN and s["badge"] == C.BADGE_UNKNOWN


def test_legacy_pending_pages_are_reported(db):
    row = _file(db, "old.pdf", [_card()], kind="pdf", status="indexed", caption_state="awaiting_vision",
                doc_meta={"scanned_pages": [1, 2, 3], "ocr_pending_pages": [1, 2, 3]})
    s = _summary(db, row)
    assert s["state"] == C.STATE_METADATA_ONLY and s["badge"] == C.BADGE_BLOCKED
    assert s["reasons"][0]["code"] == "awaiting_vision" and s["reasons"][0]["pages"] == 3


def test_metadata_only_kinds_and_encrypted(db):
    video = _file(db, "clip.mp4", [_card()], kind="video", status="metadata_only", status_reason="video")
    s = _summary(db, video)
    assert s["state"] == C.STATE_METADATA_ONLY and s["badge"] == C.BADGE_METADATA
    enc = _file(db, "locked.pdf", [_card()], kind="pdf", status="metadata_only", status_reason="encrypted")
    s = _summary(db, enc)
    assert s["badge"] == C.BADGE_METADATA and "password" in s["headline"]


def test_queued_failed_and_refreshing(db):
    new = _file(db, "new.docx", [], kind="docx", status="dirty")
    s = _summary(db, new)
    assert s["state"] == C.STATE_UNAVAILABLE and s["badge"] == C.BADGE_WAITING and not s["previewable"]
    s = _summary(db, new, in_flight=True)
    assert s["badge"] == C.BADGE_INDEXING
    failed = _file(db, "bad.pdf", [], kind="pdf", status="error", status_reason="corrupt", error="bad xref")
    s = _summary(db, failed)
    assert s["badge"] == C.BADGE_FAILED and "bad xref" in s["headline"]
    # An update queued for a file that already has text: the text stays shown.
    old = _file(db, "notes.md", [_card(), _chunk(0, "Kept text.")], kind="markdown", status="dirty")
    s = _summary(db, old)
    assert s["state"] == C.STATE_COMPLETE and s["refresh_queued"] and s["previewable"]
    assert s["badge"] == C.BADGE_INDEXED


def test_images_need_a_description(db):
    img = _file(db, "dog.jpg", [_card()], kind="image", status="indexed", caption_state="awaiting_consent")
    s = _summary(db, img)
    assert s["state"] == C.STATE_METADATA_ONLY and s["badge"] == C.BADGE_BLOCKED
    described = _file(db, "cat.jpg", [_card(), _chunk(0, "Photo — a cat.", t.CTYPE_CAPTION)], kind="image",
                      status="indexed", caption_state="done")
    s = _summary(db, described)
    assert s["state"] == C.STATE_COMPLETE and s["segments"]["image_description"] == 1


def test_text_is_readable_while_vectors_are_pending(db):
    row = _file(db, "a.md", [_card(), _chunk(0, "Alpha."), _chunk(1, "Beta.")], kind="markdown",
                status="indexed")
    s = _summary(db, row)
    assert s["state"] == C.STATE_COMPLETE
    assert s["embedding"]["state"] == "unavailable" and s["embedding"]["ready"] == 0


# ── the preview ────────────────────────────────────────────────────────────


def _pages(db, row, *, limit, max_chars=C.PREVIEW_MAX_CHARS):
    out, cursor = [], None
    while True:
        page = C.preview_page(db, row, cursor=cursor, limit=limit, max_chars=max_chars)
        out.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return out


def test_preview_reproduces_every_passage_in_order(db):
    texts = [f"Passage {i}." for i in range(7)] + ["Repeated."] * 3
    row = _file(db, "a.md", [_card()] + [_chunk(i, x) for i, x in enumerate(texts)], kind="markdown",
                status="indexed")
    pages = _pages(db, row, limit=3)
    got = [s["text"] for p in pages for s in p["segments"]]
    assert got == texts
    assert len(pages) == 4
    assert pages[0]["metadata"]["card"].startswith("File: scan.pdf")
    assert pages[0]["total_segments"] == 10
    # Duplicates are separate passages with the same token.
    tokens = [s["token"] for p in pages for s in p["segments"]]
    assert len(set(tokens)) == 8


def test_preview_splits_an_oversized_passage_without_losing_text(db):
    big = "x" * 2500 + "\n" + "y" * 1500
    row = _file(db, "a.md", [_chunk(0, "Head."), _chunk(1, big), _chunk(2, "Tail.")], kind="markdown",
                status="indexed")
    pages = _pages(db, row, limit=60, max_chars=1000)
    joined = [s for p in pages for s in p["segments"]]
    assert joined[0]["text"] == "Head."
    assert "".join(s["text"] for s in joined if s["index"] == 1) == big
    assert all(s["part"]["length"] == len(big) for s in joined if s["index"] == 1)
    assert joined[-1]["text"] == "Tail."


def test_preview_cursor_is_bound_to_the_revision(db):
    row = _file(db, "a.md", [_chunk(i, f"P{i}.") for i in range(5)], kind="markdown", status="indexed")
    first = C.preview_page(db, row, limit=2)
    assert first["next_cursor"]
    # Re-indexed: a passage changed.
    from app.documents.chunking import diff_chunks

    old = db.get_chunks(row["id"])
    new = [_chunk(i, f"P{i}!") if i == 3 else _chunk(i, f"P{i}.") for i in range(5)]
    d = diff_chunks(old, new)
    db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=d)
    with pytest.raises(C.StalePreview):
        C.preview_page(db, row, cursor=first["next_cursor"], limit=2)
    with pytest.raises(C.BadCursor):
        C.preview_page(db, row, cursor="not-a-cursor", limit=2)


def test_preview_labels_provenance(db):
    row = _file(db, "mixed.pdf", [_card(), _chunk(0, "Native page text.", page=1),
                                  _chunk(1, "Scanned page text.", t.CTYPE_OCR, page=2)],
                kind="pdf", status="indexed")
    page = C.preview_page(db, row)
    assert [s["type"] for s in page["segments"]] == ["text", "ocr"]
    assert page["segments"][1]["locator_label"] == "p. 2"


def test_coverage_record_is_deterministic():
    a = C.coverage_record(pages=4, scanned=[4, 2], ocr={2: C.OCR_DONE, 4: C.OCR_PENDING}, ocr_reason="over_cap")
    b = C.coverage_record(pages=4, scanned=[2, 4], ocr={4: C.OCR_PENDING, 2: C.OCR_DONE}, ocr_reason="over_cap")
    assert a == b == {"v": 1, "pages": 4, "scanned": [2, 4], "ocr": {"done": [2], "pending": [4]},
                      "ocr_reason": "over_cap"}
    assert C.ocr_unfinished({"extraction": a})
    assert not C.ocr_unfinished({"extraction": C.coverage_record(pages=1, scanned=[1], ocr={1: C.OCR_DONE})})
