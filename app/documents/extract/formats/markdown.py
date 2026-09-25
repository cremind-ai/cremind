"""Markdown to blocks. Used for ``.md`` files and for everything markitdown
converts (DOCX, HTML, EPUB, MSG), which arrives as Markdown too.

The two callers want different locators. A ``.md`` file is cited by line, so
``mode="lines"`` records ``line_start``/``line_end``. A converted document's
Markdown lines exist nowhere the user can open, so ``mode="structure"``
records the heading path and paragraph numbers instead.

Fenced code is tracked the way :mod:`app.cremind_documents.sections` tracks it, so a
``# comment`` inside a ``bash`` fence never becomes a heading. The tracker is
repeated here rather than imported: importing ``app.cremind_documents`` starts that
package's sync service imports, which the extractor worker must not pay for.

Large tables are split into row groups with the header repeated (see
:func:`~._base.group_rows`), so a table pasted into a document chunks like a
spreadsheet instead of arriving as one oversized block.

A Google Doc exported as Markdown is a converted document too, so its caller
asks for ``limits["md_locators"] = "structure"``. With ``limits["md_export"]``
two export habits are undone: images are inlined as base64 reference
definitions (``[image1]: <data:image/png;base64,...>``), which would be indexed
as text and eat the text budget, and punctuation is backslash-escaped
(``Điều 12\\.``), which breaks legal-heading matching. Escapes are undone per
block, after the structure is parsed, because they are what keeps ``\\# note``
from being a heading; fenced code and code spans keep theirs.
"""

from __future__ import annotations

import re
from typing import Any

from app.documents.types import ANCHOR_HARD, ANCHOR_NONE, ANCHOR_SOFT

from ._base import Ctx, decode_text, group_rows, read_text

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_RE = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_TABLE_RE = re.compile(r"^ {0,3}\|")
_TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-{1,}:?[ \t]*(\|[ \t]*:?-{1,}:?[ \t]*)*\|?[ \t]*$")
_TABLE_ROW_MIN_SPLIT = 12  # smaller tables stay one block

# Export cleanup (limits["md_export"]).
_DATA_IMAGE_DEF_RE = re.compile(
    r"^ {0,3}\[([^\]\n]{1,200})\]:[ \t]*<?data:image/[^\n]*(?:\n|\Z)", re.MULTILINE | re.IGNORECASE)
_DATA_IMAGE_INLINE_RE = re.compile(r"!\[([^\]\n]*)\]\(<?data:image/[^)\n]*\)", re.IGNORECASE)
_IMAGE_REF_RE = re.compile(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]")
# A code span (kept as is) or one escaped punctuation character.
_UNESCAPE_RE = re.compile(r"(`+)(?:.+?)\1|\\([.\-*_#+!()\[\]])", re.DOTALL)
# How far past the text budget an export is read: its base64 images are
# stripped before the budget applies to what is left.
_EXPORT_READ_FACTOR = 4


def strip_data_images(text: str) -> str:
    """Drop inlined base64 images from exported Markdown: the reference
    definitions, the ``![alt][imageN]`` uses that pointed at them (their alt
    text stays), and inline ``![alt](data:image/...)``."""
    labels: set[str] = set()

    def drop_def(m: re.Match[str]) -> str:
        labels.add(m.group(1).strip().casefold())
        return ""

    text = _DATA_IMAGE_DEF_RE.sub(drop_def, text)
    text = _DATA_IMAGE_INLINE_RE.sub(lambda m: m.group(1), text)
    if labels:
        def drop_use(m: re.Match[str]) -> str:
            label = (m.group(2) or m.group(1)).strip().casefold()
            return m.group(1) if label in labels else m.group(0)

        text = _IMAGE_REF_RE.sub(drop_use, text)
    return text


def unescape_markdown(text: str) -> str:
    """``\\.`` → ``.`` and the like, outside code spans."""
    if "\\" not in text:
        return text
    return _UNESCAPE_RE.sub(lambda m: m.group(0) if m.group(1) else m.group(2), text)


class _FenceTracker:
    """Line-by-line fenced-code state (CommonMark opener/closer rules)."""

    def __init__(self) -> None:
        self._char: str | None = None
        self._len = 0

    @property
    def open(self) -> bool:
        return self._char is not None

    def feed(self, line: str) -> bool:
        """Consume ``line``; True when it is a fence delimiter or fenced content."""
        if self._char is not None:
            stripped = line.lstrip(" ")
            if len(line) - len(stripped) <= 3:
                run = len(stripped) - len(stripped.lstrip(self._char))
                if run >= self._len and not stripped[run:].strip():
                    self._char, self._len = None, 0
            return True
        opener = _FENCE_OPEN_RE.match(line)
        if opener:
            marker, info = opener.group(1), opener.group(2)
            # A backtick fence's info string may not itself contain a backtick.
            if not (marker[0] == "`" and "`" in info):
                self._char, self._len = marker[0], len(marker)
                return True
        return False


def _role_of(first_line: str) -> str:
    if _TABLE_RE.match(first_line):
        return "table"
    if _LIST_RE.match(first_line):
        return "list"
    if _QUOTE_RE.match(first_line):
        return "quote"
    return "para"


class _Emitter:
    """Turns line spans into blocks with the right locator for ``mode``."""

    def __init__(self, ctx: Ctx, mode: str, base: dict[str, Any] | None, unescape: bool = False) -> None:
        self.ctx = ctx
        self.mode = mode
        self.base = dict(base or {})
        self.path: list[tuple[int, str]] = []
        self.para = 0
        self.unescape = unescape

    def _clean(self, text: str) -> str:
        return unescape_markdown(text) if self.unescape else text

    def _locator(self, start: int, end: int, lines: list[str]) -> dict[str, Any]:
        loc = dict(self.base)
        if self.mode == "lines":
            loc["line_start"] = start + 1
            loc["line_end"] = end
        else:
            counted = sum(1 for ln in lines if ln.strip() and not _TABLE_SEP_RE.match(ln))
            first = self.para + 1
            self.para += max(counted, 1)
            loc["heading"] = [title for _, title in self.path]
            loc["para"] = [first, self.para]
        return loc

    def heading(self, level: int, title: str, start: int, end: int) -> None:
        title = self._clean(title.strip())
        while self.path and self.path[-1][0] >= level:
            self.path.pop()
        self.path.append((level, title))
        if level == 1 and "title" not in self.ctx.result.doc_meta:
            self.ctx.result.doc_meta["title"] = title
        anchor = ANCHOR_HARD if level <= 3 else ANCHOR_SOFT
        self.ctx.add(title, anchor=anchor, level=level, role="heading",
                     locator=self._locator(start, end, [title]))

    def block(self, lines: list[str], start: int, role: str) -> None:
        if role == "table":
            self._table(lines, start)
            return
        text = "\n".join(lines)
        self.ctx.add(text if role == "code" else self._clean(text), anchor=ANCHOR_NONE, role=role,
                     locator=self._locator(start, start + len(lines), lines))

    def _table(self, lines: list[str], start: int) -> None:
        has_header = len(lines) >= 2 and bool(_TABLE_SEP_RE.match(lines[1]))
        header = lines[:2] if has_header else []
        body = list(enumerate(lines[2:] if has_header else lines))
        if len(body) < _TABLE_ROW_MIN_SPLIT:
            self.ctx.add(self._clean("\n".join(lines)), role="table",
                         locator=self._locator(start, start + len(lines), lines))
            return
        offset = start + len(header)
        rows = ((i, [text]) for i, text in body)
        counted_header = False
        for group in group_rows(rows):
            first, last = group[0][0], group[-1][0]
            chunk = [cells[0] for _, cells in group]
            # The header is repeated in every group's text, but it is one
            # paragraph of the source: count it once.
            loc = self._locator(offset + first, offset + last + 1,
                                chunk if counted_header else header + chunk)
            counted_header = True
            loc["rows"] = [first + 1, last + 1]
            self.ctx.add(self._clean("\n".join(header + chunk)), role="table", locator=loc)


def emit_markdown(ctx: Ctx, text: str, *, mode: str = "lines",
                  base: dict[str, Any] | None = None, unescape: bool = False) -> None:
    """Parse ``text`` (``\\n`` line ends) into ``ctx`` as blocks.

    Headings H1-H3 are hard anchors and H4-H6 soft ones; paragraphs are runs
    of lines between blank lines; a fenced block is one ``code`` block even
    across blank lines; a run of ``|`` lines is a table. ``unescape`` undoes
    backslash escapes in every block but code (see the module docstring).
    """
    lines = text.split("\n")
    out = _Emitter(ctx, mode, base, unescape)
    n = len(lines)
    i = 0

    # YAML front matter: a leading "---" ... "---" (or "...") block.
    if mode == "lines" and n > 1 and lines[0].strip() == "---":
        for j in range(1, min(n, 200)):
            if lines[j].strip() in ("---", "..."):
                front = lines[1:j]
                _front_matter_meta(ctx, front)
                ctx.add("\n".join(front), role="meta",
                        locator={**out.base, "line_start": 2, "line_end": j})
                i = j + 1
                break

    para: list[str] = []
    para_start = 0
    para_role = "para"

    def flush() -> None:
        nonlocal para
        if para:
            out.block(para, para_start, para_role)
            para = []

    fence = _FenceTracker()
    while i < n:
        line = lines[i]
        if fence.open or _FENCE_OPEN_RE.match(line):
            if fence.feed(line):
                flush()
                start = i
                i += 1
                while i < n and fence.open:
                    fence.feed(lines[i])
                    i += 1
                out.block(lines[start:i], start, "code")
                continue
        if not line.strip():
            flush()
            i += 1
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            out.heading(len(heading.group(1)), heading.group(2), i, i + 1)
            i += 1
            continue
        if para and para_role == "para" and _SETEXT_RE.match(line):
            level = 1 if line.strip()[0] == "=" else 2
            title = " ".join(p.strip() for p in para)
            start = para_start
            para = []
            out.heading(level, title, start, i + 1)
            i += 1
            continue
        if _HR_RE.match(line):
            flush()
            i += 1
            continue
        is_table = bool(_TABLE_RE.match(line))
        if para and (para_role == "table") != is_table:
            flush()
        if not para:
            para_start = i
            para_role = _role_of(line)
        para.append(line)
        i += 1
    flush()


def _front_matter_meta(ctx: Ctx, lines: list[str]) -> None:
    for line in lines:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip().strip("'\"")
        if not value:
            continue
        if key == "title":
            ctx.result.doc_meta.setdefault("title", value)
        elif key in ("author", "authors"):
            ctx.result.doc_meta.setdefault("author", value)
        elif key == "date":
            ctx.result.doc_meta.setdefault("created", value)


def _read_export(ctx: Ctx) -> str:
    """An exported document's text with its inlined images removed. Read past
    the text budget first, since the images are usually most of the bytes;
    :meth:`Ctx.add` still holds what is left to the budget."""
    data, truncated = ctx.read_bytes(ctx.max_text_bytes * _EXPORT_READ_FACTOR)
    if truncated:
        ctx.mark_partial()
    text, encoding = decode_text(data)
    del data
    ctx.result.doc_meta.setdefault("encoding", encoding)
    return strip_data_images(text.replace("\r\n", "\n").replace("\r", "\n"))


def extract_markdown(ctx: Ctx) -> None:
    export = bool(ctx.limits.get("md_export"))
    mode = "structure" if ctx.limits.get("md_locators") == "structure" else "lines"
    text = _read_export(ctx) if export else read_text(ctx)
    emit_markdown(ctx, text, mode=mode, unescape=export)
