"""Plain text, XML and JSON: paragraphs separated by blank lines, cited by line.

A paragraph is never split here by length. Fixed-size splits move every later
boundary when a line is inserted near the top, which would re-embed the whole
file; an oversized paragraph is left for the chunker, whose cuts depend only on
nearby content.
"""

from __future__ import annotations

import json
from typing import Any

from ._base import Ctx, read_text

# Minified JSON up to this size is re-indented so it has lines to cut at.
_PRETTY_JSON_MAX = 256 * 1024


def emit_paragraphs(ctx: Ctx, text: str, *, role: str = "para", line_locators: bool = True,
                    base: dict[str, Any] | None = None) -> None:
    lines = text.split("\n")
    start: int | None = None

    def emit(first: int, end: int) -> None:
        loc = dict(base or {})
        if line_locators:
            loc["line_start"] = first + 1
            loc["line_end"] = end
        ctx.add("\n".join(lines[first:end]), role=role, locator=loc)

    for i, line in enumerate(lines):
        if line.strip():
            if start is None:
                start = i
        elif start is not None:
            emit(start, i)
            start = None
    if start is not None:
        emit(start, len(lines))


def extract_text(ctx: Ctx) -> None:
    emit_paragraphs(ctx, read_text(ctx))


def extract_json(ctx: Ctx) -> None:
    text = read_text(ctx)
    minified = len(text) <= _PRETTY_JSON_MAX and (
        text.count("\n") < 3 or max(len(ln) for ln in text.split("\n")) > 2000)
    if minified:
        try:
            pretty = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (ValueError, RecursionError):
            pretty = None
        if pretty is not None:
            # Line numbers of the re-indented text exist nowhere on disk.
            emit_paragraphs(ctx, pretty, line_locators=False)
            return
    emit_paragraphs(ctx, text)
