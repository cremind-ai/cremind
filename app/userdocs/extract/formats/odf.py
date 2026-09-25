"""OpenDocument text, spreadsheets and presentations (odt/ods/odp).

Read straight from ``content.xml`` and ``meta.xml`` with defusedxml: the
format is plain XML in a ZIP, so no ODF library is needed, and defusedxml
refuses the entity tricks a hostile file could use to exhaust memory.

Spreadsheets repeat rows and columns by attribute (``number-rows-repeated``),
and an empty sheet routinely declares a million repeated empty rows. Empty
repeats only advance the row counter; they are never materialised.
"""

from __future__ import annotations

import zipfile
from typing import Any, Iterator

from app.userdocs.types import ANCHOR_HARD, ANCHOR_NONE, ANCHOR_SOFT

from ._base import Ctx, cell_text, iso_datetime
from .sheets import _emit_sheet_rows, _non_empty

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


# content.xml larger than this is not parsed into a tree at all.
_MAX_XML_BYTES = 256 * 1024 * 1024
# A non-empty row or cell repeated more than this is written out this often.
_MAX_REPEAT = 1000
_MAX_COLUMNS = 1024

_SKIP_TEXT = {_q("text", "note"), _q("office", "annotation"), _q("text", "tracked-changes")}
_SKIP_BODY = {
    _q("text", "table-of-content"), _q("text", "alphabetical-index"),
    _q("text", "illustration-index"), _q("text", "table-index"), _q("text", "object-index"),
    _q("text", "user-index"), _q("text", "bibliography"), _q("text", "sequence-decls"),
    _q("office", "forms"), _q("text", "tracked-changes"),
}


def _text(el: Any) -> str:
    """The visible text of an element: spans flattened, ``text:s`` expanded to
    spaces, tabs and line breaks kept, footnotes and comments left out."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = child.tag
            if tag == _q("text", "s"):
                try:
                    parts.append(" " * min(int(child.get(_q("text", "c"), "1")), 100))
                except ValueError:
                    parts.append(" ")
            elif tag == _q("text", "tab"):
                parts.append("\t")
            elif tag == _q("text", "line-break"):
                parts.append("\n")
            elif tag not in _SKIP_TEXT:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(el)
    return "".join(parts)


def _load(ctx: Ctx) -> tuple[Any, Any]:
    from defusedxml import ElementTree as ET

    with zipfile.ZipFile(ctx.source()) as zf:
        try:
            info = zf.getinfo("content.xml")
        except KeyError:
            return None, None
        if info.file_size > _MAX_XML_BYTES:
            ctx.metadata_only("too_large")
            return None, None
        content = ET.fromstring(zf.read(info))
        meta = None
        try:
            meta_info = zf.getinfo("meta.xml")
            if meta_info.file_size <= 4 * 1024 * 1024:
                meta = ET.fromstring(zf.read(meta_info))
        except KeyError:
            pass
        except Exception:  # a broken meta.xml must not cost the content
            meta = None
    return content, meta


def _read_meta(ctx: Ctx, meta: Any) -> None:
    if meta is None:
        return
    body = meta.find(_q("office", "meta"))
    if body is None:
        return
    doc_meta = ctx.result.doc_meta
    keywords: list[str] = []
    for el in body:
        text = (el.text or "").strip()
        tag = el.tag
        if tag == _q("dc", "title") and text:
            doc_meta["title"] = text
        elif tag == _q("meta", "initial-creator") and text:
            doc_meta["author"] = text
        elif tag == _q("dc", "creator") and text:
            doc_meta["last_modified_by"] = text
        elif tag == _q("meta", "creation-date") and text:
            doc_meta["created"] = iso_datetime(text)
        elif tag == _q("dc", "date") and text:
            doc_meta["modified"] = iso_datetime(text)
        elif tag == _q("dc", "subject") and text:
            doc_meta["subject"] = text
        elif tag == _q("meta", "keyword") and text:
            keywords.append(text)
        elif tag == _q("meta", "generator") and text:
            doc_meta["app"] = text
        elif tag == _q("meta", "document-statistic"):
            for attr, key in (("page-count", "pages"), ("word-count", "words")):
                value = el.get(_q("meta", attr))
                if value and value.isdigit():
                    doc_meta[key] = int(value)
    if keywords:
        doc_meta["keywords"] = ", ".join(keywords)
    if "author" not in doc_meta and doc_meta.get("last_modified_by"):
        # No initial-creator recorded: the last editor is the best proxy.
        doc_meta["author"] = doc_meta["last_modified_by"]


# ── odt ────────────────────────────────────────────────────────────────────


class _TextEmitter:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.path: list[tuple[int, str]] = []
        self.para = 0

    def _loc(self, count: int) -> dict[str, Any]:
        first = self.para + 1
        self.para += max(count, 1)
        return {"heading": [t for _, t in self.path], "para": [first, self.para]}

    def heading(self, level: int, title: str) -> None:
        title = " ".join(title.split())
        if not title:
            return
        while self.path and self.path[-1][0] >= level:
            self.path.pop()
        self.path.append((level, title))
        anchor = ANCHOR_HARD if level <= 3 else ANCHOR_SOFT
        self.ctx.add(title, anchor=anchor, level=level, role="heading", locator=self._loc(1))

    def block(self, lines: list[str], role: str) -> None:
        lines = [ln for ln in lines if ln.strip()]
        if lines:
            self.ctx.add("\n".join(lines), anchor=ANCHOR_NONE, role=role, locator=self._loc(len(lines)))

    def walk(self, container: Any) -> None:
        for el in container:
            tag = el.tag
            if tag in _SKIP_BODY:
                continue
            if tag == _q("text", "h"):
                try:
                    level = int(el.get(_q("text", "outline-level"), "1"))
                except ValueError:
                    level = 1
                self.heading(min(max(level, 1), 6), _text(el))
            elif tag == _q("text", "p"):
                self.block([_text(el)], "para")
            elif tag == _q("text", "list"):
                self.block(["- " + " ".join(_text(p).split()) for p in el.iter()
                            if p.tag in (_q("text", "p"), _q("text", "h"))], "list")
            elif tag == _q("table", "table"):
                rows = []
                for row in el.iter(_q("table", "table-row")):
                    cells = [" ".join(_text(c).split()) for c in row
                             if c.tag in (_q("table", "table-cell"), _q("table", "covered-table-cell"))]
                    if any(cells):
                        rows.append(" | ".join(cells))
                self.block(rows, "table")
            elif tag == _q("text", "section"):
                self.walk(el)


def extract_odt(ctx: Ctx) -> None:
    content, meta = _load(ctx)
    _read_meta(ctx, meta)
    if content is None:
        return
    body = content.find(f"{_q('office', 'body')}/{_q('office', 'text')}")
    if body is not None:
        _TextEmitter(ctx).walk(body)


# ── ods ────────────────────────────────────────────────────────────────────


def _repeat(el: Any, attr: str) -> int:
    try:
        return max(1, int(el.get(_q("table", attr), "1")))
    except ValueError:
        return 1


def _sheet_rows(table: Any) -> Iterator[tuple[int, list[str]]]:
    number = 0
    for row in table.iter(_q("table", "table-row")):
        cells: list[str] = []
        for cell in row:
            if cell.tag not in (_q("table", "table-cell"), _q("table", "covered-table-cell")):
                continue
            value = cell_text("\n".join(_text(p) for p in cell.findall(_q("text", "p"))))
            span = _repeat(cell, "number-columns-repeated")
            cells.extend([value] * min(span, max(0, _MAX_COLUMNS - len(cells))))
        repeat = _repeat(row, "number-rows-repeated")
        if not any(cells):
            number += repeat  # the million-empty-rows tail costs nothing
            continue
        for _ in range(min(repeat, _MAX_REPEAT)):
            number += 1
            yield number, cells
        number += max(0, repeat - _MAX_REPEAT)


def extract_ods(ctx: Ctx) -> None:
    content, meta = _load(ctx)
    _read_meta(ctx, meta)
    if content is None:
        return
    body = content.find(f"{_q('office', 'body')}/{_q('office', 'spreadsheet')}")
    if body is None:
        return
    tables = body.findall(_q("table", "table"))
    ctx.result.doc_meta["sheets"] = len(tables)
    max_rows = ctx.limit("max_rows_per_sheet")
    for index, table in enumerate(tables, start=1):
        title = table.get(_q("table", "name")) or f"Sheet{index}"
        ctx.add(title, anchor=ANCHOR_HARD, level=1, role="heading", locator={"sheet": title})
        _emit_sheet_rows(ctx, _non_empty(_sheet_rows(table)), sheet=title, max_rows=max_rows)


# ── odp ────────────────────────────────────────────────────────────────────


def _slide_texts(node: Any, into: list[str], notes: list[str]) -> None:
    """Paragraph texts under ``node`` into ``into``, speaker notes into ``notes``."""
    for child in node:
        if child.tag == _q("presentation", "notes"):
            _slide_texts(child, notes, notes)
        elif child.tag in (_q("text", "p"), _q("text", "h")):
            text = " ".join(_text(child).split())
            if text:
                into.append(text)
        else:
            _slide_texts(child, into, notes)


def extract_odp(ctx: Ctx) -> None:
    content, meta = _load(ctx)
    _read_meta(ctx, meta)
    if content is None:
        return
    body = content.find(f"{_q('office', 'body')}/{_q('office', 'presentation')}")
    if body is None:
        return
    pages = body.findall(_q("draw", "page"))
    ctx.result.doc_meta["slides"] = len(pages)
    max_slides = ctx.limit("max_pages")
    for number, page in enumerate(pages, start=1):
        if number > max_slides:
            ctx.mark_partial()
            break
        parts: list[str] = []
        notes: list[str] = []
        _slide_texts(page, parts, notes)
        if notes:
            parts.append("Notes: " + "\n".join(notes))
        if parts:
            ctx.add("\n".join(parts), anchor=ANCHOR_HARD, role="para", locator={"slide": number})
