"""Scanned PDFs through the real engine, with a fake vision model.

What these pin (the reported failure: a scanned decree indexed as nothing but
its file card, rejected by research as "not a legal document"):

- a scan waits for consent with its pages listed as waiting — the file is
  partly indexed (its native pages), never "indexed" as if complete;
- once consent is given, the pages are transcribed and chunked TOGETHER with
  the native text, in page order, so the decree's articles, number and legal
  metadata are found exactly as for a native decree;
- a confirmed blank page is not a failure; an empty answer is (and is
  retried); a transcription cut at the output limit is reported;
- a long scan is transcribed batch after batch — never the first pages again
  — and no page is ever sent twice (a re-index reuses every transcription);
- a copy of an unfinished scan does not borrow its incomplete chunks;
- PDFs read by the older extractor are queued once for repair.
"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest

pytest.importorskip("a2a")
PIL_Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pypdfium2")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.documents import content as C  # noqa: E402
from app.documents import runtime as rt_module  # noqa: E402
from app.documents.vision import captioner, resolver  # noqa: E402

from .test_engine_e2e import _files, _idle, _start, _wait, env  # noqa: E402,F401

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def _esc(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _level(page: int) -> int:
    return (30 + 40 * page) % 250


def scan_pdf(pages: list[list[str] | None]) -> bytes:
    """A PDF whose page ``i`` shows the given text lines, or — for None — is
    one full-page image of its own grey level (so a vision fake can tell the
    pages apart)."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for number, lines in enumerate(pages, start=1):
        if lines is None:
            img = bytes([_level(number)]) * 256
            objects.append(b"<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceGray "
                           b"/BitsPerComponent 8 /Length 256 >>\nstream\n" + img + b"\nendstream")
            res = b"/XObject << /Im1 %d 0 R >>" % len(objects)
            content = b"q 612 0 0 792 0 0 cm /Im1 Do Q\n"
        else:
            res = b"/Font << /F1 3 0 R >>"
            content = b"".join(b"BT /F1 11 Tf 72 %d Td (" % (720 - 20 * i) + _esc(t) + b") Tj ET\n"
                               for i, t in enumerate(lines))
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        cid = len(objects)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << " + res
                       + b" >> /Contents %d 0 R >>" % cid)
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _page_of(messages) -> int:
    for part in messages[-1]["content"]:
        if part.get("type") == "image_url":
            data = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
            with PIL_Image.open(BytesIO(data)) as im:
                grey = im.convert("L").getpixel((0, 0))
            return min(range(1, 7), key=lambda p: abs(_level(p) - grey))
    return 0


class FakeOCR:
    """Transcribes page ``p`` as ``pages[p]`` (a string, or a callable of the
    attempt number); counts calls per page."""

    pages: dict[int, object] = {}
    calls: dict[int, int] = {}
    tokens_out: dict[int, int] = {}

    async def chat_completion(self, **kw):
        page = _page_of(kw["messages"])
        FakeOCR.calls[page] = FakeOCR.calls.get(page, 0) + 1
        answer = FakeOCR.pages.get(page, "")
        if callable(answer):
            answer = answer(FakeOCR.calls[page])
        if answer:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": answer}
        yield {"type": ChatCompletionTypeEnum.DONE,
               "usage": {"input_tokens": 50, "output_tokens": FakeOCR.tokens_out.get(page, 40)}}


@pytest.fixture
def ocr(env, monkeypatch):
    FakeOCR.pages, FakeOCR.calls, FakeOCR.tokens_out = {}, {}, {}
    monkeypatch.setattr(resolver, "resolve_dedicated_vision",
                        lambda profile: resolver.VisionResolution(True, "openai", "gpt-4o"))
    monkeypatch.setattr(resolver, "build_vision_llm", lambda profile, res: FakeOCR())
    import app.lib.llm.base as base

    monkeypatch.setattr(base, "done_chunk_token_usage", lambda resp: resp.get("usage") or {})

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(captioner, "_record_usage", no_usage)
    monkeypatch.setattr(rt_module.ProfileRuntime, "caption_cap", lambda self: 100)
    return FakeOCR


def _consent(env):
    env.svc.control("alice", "consent_vision", model="openai/gpt-4o")


def _row(rt, name):
    return _files(rt)[name]


def _chunks(rt, row):
    return rt.db.chunk_rows([c.id for c in rt.db.get_chunks(int(row["id"]))])


def _summary(rt, row):
    stats = C.chunk_stats(rt.db, [int(row["id"])], gen=C.active_gen(rt.db))[int(row["id"])]
    return C.summarize(row, stats, gen=C.active_gen(rt.db))


DECREE_PAGES: list[list[str] | None] = [
    ["GOVERNMENT", "No.: 165/2024/ND-CP", "DECREE", "On the handling of violations in road traffic"],
    None, None, None,
    ["Article 5. Effect", "This Decree takes effect on January 1, 2025."],
]
OCR_TEXT = {
    2: "Article 1. Scope\nThis Decree governs road traffic.\n\nArticle 2. Subjects\nIt applies to drivers.",
    3: "Article 3. Terms\nAlcohol concentration is measured in breath.\n\nArticle 4. Patrol\n"
       "1. Traffic police may stop vehicles to check alcohol concentration.",
    4: "[BLANK PAGE]",
}


def test_a_scan_waits_for_consent_then_is_transcribed_merged_and_legal(env, ocr):
    ocr.pages = dict(OCR_TEXT)
    (env.alice / "decree.pdf").write_bytes(scan_pdf(DECREE_PAGES))
    _start(env, "alice")
    rt = _idle(env, "alice")
    row = _row(rt, "decree.pdf")
    # Waiting: the native pages are readable, the scans are listed as waiting.
    assert row["caption_state"] == "awaiting_consent" and row["status"] == "indexed"
    rec = row["doc_meta"]["extraction"]
    assert rec["text_pages"] == [1, 5] and rec["scanned"] == [2, 3, 4]
    assert rec["ocr"] == {"pending": [2, 3, 4]} and rec["ocr_reason"] == "awaiting_consent"
    s = _summary(rt, row)
    assert s["state"] == C.STATE_PARTIAL and s["pages"]["pending"] == 3
    assert "3 pages are waiting for OCR" in s["headline"]
    assert ocr.calls == {}

    _consent(env)
    _wait(lambda: rt.requeue_waiting_vision() >= 0 and _row(rt, "decree.pdf")["caption_state"] == "done", 60)
    rt = _idle(env, "alice")
    row = _row(rt, "decree.pdf")
    assert ocr.calls == {2: 1, 3: 1, 4: 1}
    rec = row["doc_meta"]["extraction"]
    assert rec["ocr"] == {"blank": [4], "done": [2, 3]} and "ocr_reason" not in rec
    # Merged with the native text: the legal overlay saw every article, the
    # number was read from the native header, and reading order is page order.
    assert row["doc_meta"]["legal"]["number"] == "165/2024/ND-CP"
    chunks = [c for c in _chunks(rt, row) if c["ctype"] != "file_card"]
    keys = [c["section_key"] for c in chunks if c["section_key"]]
    assert {"art:1", "art:2", "art:3", "art:4", "art:5"} <= {k.split("/")[0] for k in keys}
    pages = [c["locator"].get("page") for c in chunks]
    assert pages == sorted(pages)
    ocr_chunk = next(c for c in chunks if "stop vehicles" in c["text"])
    assert ocr_chunk["ctype"] == "ocr" and ocr_chunk["section_key"].startswith("art:4")
    assert next(c for c in chunks if "takes effect" in c["text"])["ctype"] == "body"
    s = _summary(rt, row)
    assert s["state"] == C.STATE_COMPLETE and s["pages"]["blank"] == 1

    # A re-index reads the file again and reuses every transcription.
    env.svc.control("alice", "reindex", targets=[row["cite_id"]])
    _wait(lambda: _row(rt, "decree.pdf")["status"] == "indexed" and not rt.in_flight, 60)
    _idle(env, "alice")
    assert ocr.calls == {2: 1, 3: 1, 4: 1}


def test_a_long_scan_is_transcribed_batch_after_batch(env, ocr, monkeypatch):
    ocr.pages = {p: f"Page {p} text of the scanned report." for p in range(1, 6)}
    monkeypatch.setattr(env.svc, "extract_limits", lambda: {"max_ocr_pages": 2})
    (env.alice / "report.pdf").write_bytes(scan_pdf([None] * 5))
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    row = _row(rt, "report.pdf")
    rec = row["doc_meta"]["extraction"]
    assert rec["ocr"] == {"done": [1, 2], "pending": [3, 4, 5]} and rec["ocr_reason"] == "next_batch"
    assert row["caption_state"] == "next_batch"
    assert _summary(rt, row)["state"] == C.STATE_PARTIAL
    _wait(lambda: rt.requeue_waiting_vision() >= 0 and _row(rt, "report.pdf")["caption_state"] == "done", 90)
    rt = _idle(env, "alice")
    row = _row(rt, "report.pdf")
    assert row["doc_meta"]["extraction"]["ocr"] == {"done": [1, 2, 3, 4, 5]}
    # Every page was sent exactly once across the batches.
    assert ocr.calls == {1: 1, 2: 1, 3: 1, 4: 1, 5: 1}
    texts = " ".join(c["text"] for c in _chunks(rt, row))
    assert all(f"Page {p} text" in texts for p in range(1, 6))


def test_an_empty_answer_is_a_failure_that_retry_repairs(env, ocr):
    ocr.pages = {1: lambda attempt: "" if attempt == 1 else "Recovered text of page one.",
                 2: "Page two text."}
    (env.alice / "memo.pdf").write_bytes(scan_pdf([None, None]))
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    row = _row(rt, "memo.pdf")
    assert row["doc_meta"]["extraction"]["ocr"] == {"done": [2], "failed": [1]}
    assert row["caption_state"] == "failed"
    s = _summary(rt, row)
    assert s["state"] == C.STATE_PARTIAL and any(r["code"] == "ocr_failed" for r in s["reasons"])
    assert env.svc.control("alice", "retry_failed")["files"] == 1
    _wait(lambda: _row(rt, "memo.pdf")["caption_state"] == "done", 60)
    rt = _idle(env, "alice")
    assert "Recovered text of page one." in " ".join(c["text"] for c in _chunks(rt, _row(rt, "memo.pdf")))
    assert ocr.calls == {1: 2, 2: 1}


def test_a_cut_off_transcription_is_reported(env, ocr):
    ocr.pages = {1: "A very long page " * 20}
    ocr.tokens_out = {1: captioner.OCR_MAX_TOKENS}
    (env.alice / "dense.pdf").write_bytes(scan_pdf([None]))
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    row = _row(rt, "dense.pdf")
    assert row["doc_meta"]["extraction"]["ocr"] == {"truncated": [1]}
    s = _summary(rt, row)
    assert s["state"] == C.STATE_PARTIAL and any(r["code"] == "ocr_truncated" for r in s["reasons"])


def test_a_copy_of_an_unfinished_scan_does_its_own_ocr(env, ocr):
    ocr.pages = dict(OCR_TEXT)
    pdf = scan_pdf(DECREE_PAGES)
    (env.alice / "decree.pdf").write_bytes(pdf)
    (env.alice / "decree copy.pdf").write_bytes(pdf)
    _start(env, "alice")
    rt = _idle(env, "alice")
    for name in ("decree.pdf", "decree copy.pdf"):
        assert _row(rt, name)["caption_state"] == "awaiting_consent"
    _consent(env)
    _wait(lambda: rt.requeue_waiting_vision() >= 0 and all(
        _row(rt, n)["caption_state"] == "done" for n in ("decree.pdf", "decree copy.pdf")), 90)
    rt = _idle(env, "alice")
    for name in ("decree.pdf", "decree copy.pdf"):
        assert _row(rt, name)["doc_meta"]["extraction"]["ocr"] == {"blank": [4], "done": [2, 3]}
    # The same bytes: every page was transcribed once for both copies.
    assert ocr.calls == {2: 1, 3: 1, 4: 1}


def test_pdfs_read_by_the_older_extractor_are_queued_once(env, ocr):
    ocr.pages = dict(OCR_TEXT)
    (env.alice / "decree.pdf").write_bytes(scan_pdf(DECREE_PAGES))
    (env.alice / "notes.md").write_text("# Notes\n\nNothing scanned here.", encoding="utf-8")
    env.enable("alice", env.alice)
    _consent(env)
    _start(env, "alice")
    rt = _idle(env, "alice")
    pdf = _row(rt, "decree.pdf")
    assert pdf["extractor_version"] == 2 and _row(rt, "notes.md")["extractor_version"] == 1
    # As an older build left it.
    rt.db.update_file(int(pdf["id"]), extractor_version=1)
    assert rt.queue_stale_extraction("local") == 1
    _wait(lambda: _row(rt, "decree.pdf")["extractor_version"] == 2 and not rt.in_flight, 60)
    _idle(env, "alice")
    assert rt.queue_stale_extraction("local") == 0


# ── the merge, without the engine ──────────────────────────────────────────


def test_merge_puts_transcriptions_in_page_order_with_provenance():
    from app.documents import types as t
    from app.documents.chunking.chunker import SRC_OCR

    native = [t.Block(text="Header", locator={"page": 1}), t.Block(text="Closing", locator={"page": 4})]
    merged = rt_module.merge_ocr_blocks(native, {2: "Scanned two\n\nMore of two", 3: "Scanned three"})
    assert [b.text for b in merged] == ["Header", "Scanned two", "More of two", "Scanned three", "Closing"]
    assert [b.locator.get("src") for b in merged] == [None, SRC_OCR, SRC_OCR, SRC_OCR, None]
    assert merged[1].anchor == t.ANCHOR_HARD and merged[3].anchor == t.ANCHOR_SOFT
    assert merged[4].anchor == t.ANCHOR_HARD


def test_ocr_outcomes():
    assert captioner.ocr_outcome("") == "empty"
    assert captioner.ocr_outcome("  [BLANK PAGE] ") == "blank"
    assert captioner.ocr_outcome("Blank page.") == "blank"
    assert captioner.ocr_outcome("text", captioner.OCR_MAX_TOKENS) == "truncated"
    assert captioner.ocr_outcome("Article 1. Scope", 200) == "text"
