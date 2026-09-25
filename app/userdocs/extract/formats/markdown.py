"""Markdown to blocks. Used for ``.md`` files and for everything markitdown
converts (DOCX, HTML, EPUB, MSG), which arrives as Markdown too.

The two callers want different locators. A ``.md`` file is cited by line, so
``mode="lines"`` records ``line_start``/``line_end``. A converted document's
Markdown lines exist nowhere the user can open, so ``mode="structure"``
records the heading path and paragraph numbers instead.

Fenced code is tracked the way :mod:`app.documents.sections` tracks it, so a
``# comment`` inside a ``bash`` fence never becomes a heading. The tracker is
repeated here rather than imported: importing ``app.documents`` starts that
package's sync service imports, which the extractor worker must not pay for.

Large tables are split into row groups with the header repeated (see
:func:`~._base.group_rows`), so a table pasted into a document chunks like a
spreadsheet instead of arriving as one oversized block.
"""

from __future__ import annotations

import re
from typing import Any

from app.userdocs.types import ANCHOR_HARD, ANCHOR_NONE, ANCHOR_SOFT

from ._base import Ctx, group_rows, read_text

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST_RE = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_TABLE_RE = re.compile(r"^ {0,3}\|")
_TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-{1,}:?[ \t]*(\|[ \t]*:?-{1,}:?[ \t]*)*\|?[ \t]*$")
_TABLE_ROW_MIN_SPLIT = 12  # smaller tables stay one block


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

    def __init__(self, ctx: Ctx, mode: str, base: dict[str, Any] | None) -> None:
        self.ctx = ctx
        self.mode = mode
        self.base = dict(base or {})
        self.path: list[tuple[int, str]] = []
        self.para = 0

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
        title = title.strip()
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
        self.ctx.add("\n".join(lines), anchor=ANCHOR_NONE, role=role,
                     locator=self._locator(start, start + len(lines), lines))

    def _table(self, lines: list[str], start: int) -> None:
        has_header = len(lines) >= 2 and bool(_TABLE_SEP_RE.match(lines[1]))
        header = lines[:2] if has_header else []
        body = list(enumerate(lines[2:] if has_header else lines))
        if len(body) < _TABLE_ROW_MIN_SPLIT:
            self.ctx.add("\n".join(lines), role="table",
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
            self.ctx.add("\n".join(header + chunk), role="table", locator=loc)


def emit_markdown(ctx: Ctx, text: str, *, mode: str = "lines",
                  base: dict[str, Any] | None = None) -> None:
    """Parse ``text`` (``\\n`` line ends) into ``ctx`` as blocks.

    Headings H1-H3 are hard anchors and H4-H6 soft ones; paragraphs are runs
    of lines between blank lines; a fenced block is one ``code`` block even
    across blank lines; a run of ``|`` lines is a table.
    """
    lines = text.split("\n")
    out = _Emitter(ctx, mode, base)
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


def extract_markdown(ctx: Ctx) -> None:
    emit_markdown(ctx, read_text(ctx), mode="lines")
