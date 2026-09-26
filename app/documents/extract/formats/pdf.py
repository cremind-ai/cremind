"""PDF: text page by page with pdfplumber, headings by font size, and scanned
pages rendered for OCR.

**Headings.** A PDF has no heading markup, only glyph sizes. A short line
whose largest glyph is more than 1.2 times the page's median size is a heading
candidate. Candidate sizes are ranked across the whole document, largest
first: the top two ranks are hard anchors (levels 1-2), the rest soft. The
ranking has to be document-wide, so pages are collected first and blocks
emitted after.

**Running headers and footers.** A line repeated at the top or bottom of many
pages ("Company confidential", "Page 3 of 40") would otherwise land in every
chunk and, if set in a larger font, become a hard anchor on every page. Lines
among a page's first or last two whose text (digits folded) recurs on at
least 30% of pages are dropped.

**Scanned pages.** A page with under 25 characters of text where images
cover at least half the page is a scan. It is rendered with pypdfium2 at
144 dpi (long side capped at 2400 px) and returned in ``ocr_pages`` for the
vision model. Rendering happens here because the worker already holds the
file and the parent must never parse a PDF.

**Encryption.** pdfminer opens files protected by an owner password only
(the empty user password) by itself. A file that needs a user password is
``metadata_only(encrypted)``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.documents.types import ANCHOR_HARD, ANCHOR_NONE, ANCHOR_SOFT

from ._base import Ctx

_HEADING_RATIO = 1.2
_HEADING_MAX_CHARS = 120
_HEADING_MAX_WORDS = 15
_HARD_HEADING_RANKS = 2
_SCAN_MAX_CHARS = 25
_SCAN_MIN_COVER = 0.5
_OCR_DPI = 144
_OCR_MAX_SIDE = 2400
_RUNNING_MIN_PAGES = 3
_RUNNING_SHARE = 0.3
_EDGE_LINES = 2

_PASSWORD_ERRORS = {"PDFPasswordIncorrect", "PDFEncryptionError"}
_PDF_DATE_RE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*(?:(Z)|([+-])(\d{2})'?(\d{2})?'?)?")
_DIGITS_RE = re.compile(r"\d+")


@dataclass
class _Line:
    text: str
    top: float
    bottom: float
    size: float
    candidate: bool


def pdf_date_to_iso(value: Any) -> str | None:
    """``D:YYYYMMDDHHmmSS+HH'mm'`` (any tail may be missing) as ISO 8601."""
    text = _pdf_str(value)
    if not text:
        return None
    m = _PDF_DATE_RE.match(text.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, zulu, sign, tzh, tzm = m.groups()
    try:
        tz = None
        if zulu:
            tz = timezone.utc
        elif sign:
            delta = timedelta(hours=int(tzh), minutes=int(tzm or 0))
            tz = timezone(delta if sign == "+" else -delta)
        stamp = datetime(int(year), int(month or 1), int(day or 1), int(hour or 0),
                         int(minute or 0), int(second or 0), tzinfo=tz)
    except ValueError:
        return None
    return stamp.isoformat()


def _pdf_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        if value.startswith((b"\xfe\xff", b"\xff\xfe")):
            value = value.decode("utf-16", "replace")
        else:
            value = value.decode("latin-1")
    elif not isinstance(value, str):
        value = getattr(value, "name", None) or str(value)  # pdfminer PSLiteral
    value = value.replace("\x00", "").strip()
    return value or None


def _is_password_error(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        current = stack.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if type(current).__name__ in _PASSWORD_ERRORS:
            return True
        # pdfplumber wraps pdfminer errors, sometimes as an argument.
        stack.extend([current.__cause__, current.__context__, *current.args])
    return False


def _has_encrypt_marker(ctx: Ctx) -> bool:
    try:
        if ctx.req.data is not None:
            tail = ctx.req.data[-8192:]
        else:
            with ctx.open() as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                tail = fh.read()
    except OSError:
        return False
    return b"/Encrypt" in tail


def _read_meta(ctx: Ctx, pdf: Any) -> None:
    info = pdf.metadata or {}
    doc_meta = ctx.result.doc_meta
    for key, field in (("Title", "title"), ("Author", "author"), ("Subject", "subject"),
                       ("Keywords", "keywords")):
        value = _pdf_str(info.get(key))
        if value:
            doc_meta[field] = value
    for key, field in (("CreationDate", "created"), ("ModDate", "modified")):
        value = pdf_date_to_iso(info.get(key))
        if value:
            doc_meta[field] = value
    app = _pdf_str(info.get("Creator")) or _pdf_str(info.get("Producer"))
    if app:
        doc_meta["app"] = app


def _page_lines(page: Any) -> tuple[list[_Line], int]:
    chars = page.chars
    visible = [c for c in chars if c.get("text", "").strip()]
    if not visible:
        return [], 0
    sizes = sorted(float(c.get("size") or 0) for c in visible)
    median = sizes[len(sizes) // 2]
    lines: list[_Line] = []
    for item in page.extract_text_lines(strip=True, return_chars=True):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        size = max((float(c.get("size") or 0) for c in item.get("chars") or []), default=0.0)
        size = round(size * 2) / 2
        candidate = (median > 0 and size > median * _HEADING_RATIO and len(text) <= _HEADING_MAX_CHARS
                     and len(text.split()) <= _HEADING_MAX_WORDS and any(ch.isalpha() for ch in text))
        lines.append(_Line(text, float(item.get("top", 0)), float(item.get("bottom", 0)), size, candidate))
    return lines, len(visible)


def _image_cover(page: Any) -> float:
    x0p, topp, x1p, bottomp = page.bbox
    area = max((x1p - x0p) * (bottomp - topp), 1.0)
    covered = 0.0
    for im in page.images:
        x0, x1 = max(im["x0"], x0p), min(im["x1"], x1p)
        top, bottom = max(im["top"], topp), min(im["bottom"], bottomp)
        if x1 > x0 and bottom > top:
            covered += (x1 - x0) * (bottom - top)
    return min(covered / area, 1.0)


def _drop_running_lines(pages: list[tuple[int, list[_Line]]]) -> None:
    if len(pages) < _RUNNING_MIN_PAGES:
        return

    def key(line: _Line) -> str:
        return _DIGITS_RE.sub("#", line.text.lower())

    counts: Counter[str] = Counter()
    for _number, lines in pages:
        edge = lines[:_EDGE_LINES] + lines[-_EDGE_LINES:]
        counts.update({key(line) for line in edge})
    threshold = max(_RUNNING_MIN_PAGES, _RUNNING_SHARE * len(pages))
    running = {k for k, n in counts.items() if n >= threshold}
    if not running:
        return
    for index, (number, lines) in enumerate(pages):
        n = len(lines)
        kept = [line for i, line in enumerate(lines)
                if not ((i < _EDGE_LINES or i >= n - _EDGE_LINES) and key(line) in running)]
        pages[index] = (number, kept)


def _render_ocr(ctx: Ctx, numbers: list[int]) -> None:
    ctx.result.doc_meta["scanned_pages"] = numbers
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return  # scanned_pages still tells the engine what it could not read
    cap = ctx.limit("max_ocr_pages")
    budget = ctx.limit("max_ocr_mb") * 1024 * 1024
    used = 0
    source: Any = ctx.req.data if ctx.req.data is not None else ctx.req.path
    doc = pdfium.PdfDocument(source)
    try:
        for number in numbers:
            if len(ctx.result.ocr_pages) >= cap or used >= budget:
                ctx.mark_partial()
                break
            page = doc[number - 1]
            try:
                width, height = page.get_size()
                scale = min(_OCR_DPI / 72.0, _OCR_MAX_SIDE / max(width, height, 1.0))
                bitmap = page.render(scale=scale)
                try:
                    buf = io.BytesIO()
                    bitmap.to_pil().save(buf, format="PNG")
                    png = buf.getvalue()
                finally:
                    bitmap.close()
            finally:
                page.close()
            used += len(png)
            ctx.result.ocr_pages.append({
                "page": number,
                "sha256": hashlib.sha256(png).hexdigest(),
                "png_b64": base64.b64encode(png).decode("ascii"),
            })
    finally:
        doc.close()


def _emit_page(ctx: Ctx, number: int, lines: list[_Line], rank: dict[float, int]) -> None:
    """One page's lines as heading and paragraph blocks. The page's first
    block after page 1 is at least a soft anchor: the page break."""
    first = True
    para: list[tuple[int, _Line]] = []

    def add(text: str, anchor: int, start: int, end: int, **kw: Any) -> None:
        nonlocal first
        if first and number > 1:
            anchor = max(anchor, ANCHOR_SOFT)
        first = False
        ctx.add(text, anchor=anchor, locator={"page": number, "line_start": start, "line_end": end}, **kw)

    def flush() -> None:
        if para:
            add("\n".join(line.text for _, line in para), ANCHOR_NONE, para[0][0], para[-1][0])
            para.clear()

    for index, line in enumerate(lines, start=1):
        if line.candidate:
            flush()
            level = min(rank[line.size], 6)
            add(line.text, ANCHOR_HARD if level <= _HARD_HEADING_RANKS else ANCHOR_SOFT, index, index,
                level=level, role="heading")
            continue
        if para:
            prev = para[-1][1]
            height = max(prev.bottom - prev.top, 1.0)
            if line.top - prev.bottom > 0.8 * height or abs(line.size - prev.size) > 1.0:
                flush()
        para.append((index, line))
    flush()


def _emit(ctx: Ctx, pages: list[tuple[int, list[_Line]]]) -> None:
    sizes = sorted({line.size for _n, lines in pages for line in lines if line.candidate}, reverse=True)
    rank = {size: i + 1 for i, size in enumerate(sizes)}
    for number, lines in pages:
        _emit_page(ctx, number, lines, rank)


def extract_pdf(ctx: Ctx) -> None:
    import pdfplumber

    try:
        pdf = pdfplumber.open(ctx.source(), raise_unicode_errors=False)
    except Exception as exc:
        if _is_password_error(exc) or _has_encrypt_marker(ctx):
            ctx.metadata_only("encrypted")
            return
        raise
    pages: list[tuple[int, list[_Line]]] = []
    scanned: list[int] = []
    with pdf:
        _read_meta(ctx, pdf)
        all_pages = pdf.pages
        ctx.result.doc_meta["pages"] = len(all_pages)
        max_pages = ctx.limit("max_pages")
        if len(all_pages) > max_pages:
            ctx.mark_partial()
        budget = ctx.budget_left
        broken: list[int] = []
        for page in all_pages[:max_pages]:
            try:
                lines, visible = _page_lines(page)
                if visible < _SCAN_MAX_CHARS and page.images and _image_cover(page) >= _SCAN_MIN_COVER:
                    scanned.append(page.page_number)
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 - one damaged page must not cost the others
                broken.append(page.page_number)
                continue
            finally:
                page.close()  # drop pdfplumber's per-page object cache
            pages.append((page.page_number, lines))
            budget -= sum(len(line.text) + 1 for line in lines)
            if budget <= 0:
                ctx.mark_partial()
                break
        if broken:
            ctx.result.doc_meta["unreadable_pages"] = broken
            ctx.mark_partial("corrupt")
    if scanned:
        _render_ocr(ctx, scanned)
    _drop_running_lines(pages)
    _emit(ctx, pages)
