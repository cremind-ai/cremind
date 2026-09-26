"""Source code: blocks that follow definitions, cited by line.

A top-level function or class is a soft anchor, so a chunk prefers to start
at one. Python is read with :mod:`ast` (exact spans, decorators included);
other languages use a line-start regex for the usual definition keywords.
Comment and annotation lines directly above a definition go with it, because
that is where its explanation lives.

A definition longer than a chunk (about 60 lines) is emitted as several
blocks: a class at its methods, anything else at blank lines, with only the
first piece anchored. The chunker could split the one big block itself, but
it has no sentence boundaries to use in code and would fall back to a rolling
hash; blank lines and method boundaries are better cut points.
"""

from __future__ import annotations

import ast
import re

from app.documents.types import ANCHOR_NONE, ANCHOR_SOFT

from ._base import Ctx, read_text

_LONG_SPAN = 60
_PY_EXTS = (".py", ".pyw", ".pyi")

_ANCHOR_RE = re.compile(r"""^[ \t]{0,4}(?:
      (?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function\b
    | (?:export[ \t]+)?(?:default[ \t]+)?(?:(?:abstract|final|sealed|data|open|static)[ \t]+)*class\b
    | (?:pub(?:\([\w:]+\))?[ \t]+)?(?:async[ \t]+)?(?:unsafe[ \t]+)?(?:const[ \t]+)?fn\b
    | func\b | impl\b | def\b
    | (?:export[ \t]+)?(?:pub[ \t]+)?(?:struct|enum|trait|interface|module|namespace|object|protocol|extension)\b
    | (?:public|private|protected|internal)\b[^;=\n]*\(
)""", re.X)
# Lines that belong to the definition below them: comments, doc comments,
# decorators and annotations.
_PREAMBLE_RE = re.compile(r"^[ \t]*(?://|/\*|\*|#|@|--)")


def _attach_preamble(lines: list[str], start: int, floor: int) -> int:
    """Move ``start`` up over comment/annotation lines directly above it."""
    while start - 1 >= floor and lines[start - 1].strip() and _PREAMBLE_RE.match(lines[start - 1]):
        start -= 1
    return start


def _emit_region(ctx: Ctx, lines: list[str], start: int, end: int, anchor: int) -> None:
    """``lines[start:end]`` as blank-line-separated blocks; only the first
    gets ``anchor``."""
    para: int | None = None
    for i in range(start, end + 1):
        blank = i == end or not lines[i].strip()
        if not blank and para is None:
            para = i
        elif blank and para is not None:
            ctx.add("\n".join(lines[para:i]), anchor=anchor, role="code",
                    locator={"line_start": para + 1, "line_end": i})
            anchor = ANCHOR_NONE
            para = None


def _emit_definition(ctx: Ctx, lines: list[str], start: int, end: int,
                     members: list[tuple[int, int]]) -> None:
    """One definition spanning ``lines[start:end]``; ``members`` are its
    methods' spans when it is a class."""
    while end > start and not lines[end - 1].strip():
        end -= 1
    if end - start <= _LONG_SPAN:
        ctx.add("\n".join(lines[start:end]), anchor=ANCHOR_SOFT, role="code",
                locator={"line_start": start + 1, "line_end": end})
        return
    if not members:
        _emit_region(ctx, lines, start, end, ANCHOR_SOFT)
        return
    cursor, anchor = start, ANCHOR_SOFT
    for m_start, m_end in members:
        if m_start > cursor:
            _emit_region(ctx, lines, cursor, m_start, anchor)
            anchor = ANCHOR_NONE
        if m_end - m_start <= _LONG_SPAN:
            ctx.add("\n".join(lines[m_start:m_end]), anchor=anchor, role="code",
                    locator={"line_start": m_start + 1, "line_end": m_end})
        else:
            _emit_region(ctx, lines, m_start, m_end, anchor)
        anchor = ANCHOR_NONE
        cursor = m_end
    if cursor < end:
        _emit_region(ctx, lines, cursor, end, ANCHOR_NONE)


def _python_imports(tree: ast.Module) -> tuple[list[str], int, int]:
    """Imported module names (top level, plus one level into if/try), and the
    first and last line holding one."""
    names: list[str] = []
    first, last = 0, 0

    def visit(stmts: list[ast.stmt], nested: bool) -> None:
        nonlocal first, last
        for node in stmts:
            found: list[str] = []
            if isinstance(node, ast.Import):
                found = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                found = ["." * node.level + (node.module or "")]
            elif not nested and isinstance(node, (ast.If, ast.Try)):
                visit(node.body, True)
                visit(node.orelse, True)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, True)
                visit(getattr(node, "finalbody", []), True)
            if found:
                names.extend(found)
                first = node.lineno if not first else min(first, node.lineno)
                last = max(last, node.end_lineno or node.lineno)

    visit(tree.body, False)
    return list(dict.fromkeys(n for n in names if n)), first, last


def _python_spans(tree: ast.Module, lines: list[str]) -> list[tuple[int, int, list[tuple[int, int]]]]:
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    spans: list[tuple[int, int, list[tuple[int, int]]]] = []
    floor = 0
    for node in tree.body:
        if not isinstance(node, defs):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        start = _attach_preamble(lines, start, floor)
        end = node.end_lineno or node.lineno
        members: list[tuple[int, int]] = []
        if isinstance(node, ast.ClassDef):
            inner_floor = start + 1
            for child in node.body:
                if isinstance(child, defs):
                    m_start = min([child.lineno] + [d.lineno for d in child.decorator_list]) - 1
                    m_start = _attach_preamble(lines, m_start, inner_floor)
                    m_end = child.end_lineno or child.lineno
                    members.append((m_start, m_end))
                    inner_floor = m_end
        spans.append((start, end, members))
        floor = end
    return spans


def _emit_python(ctx: Ctx, text: str, lines: list[str]) -> bool:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False  # not valid Python (or Python 2): the regex path still works
    imports, first, last = _python_imports(tree)
    if imports:
        ctx.add("imports: " + ", ".join(imports), role="meta",
                locator={"line_start": first, "line_end": last})
    cursor = 0
    for start, end, members in _python_spans(tree, lines):
        if start > cursor:
            _emit_region(ctx, lines, cursor, start, ANCHOR_NONE)
        _emit_definition(ctx, lines, start, end, members)
        cursor = end
    if cursor < len(lines):
        _emit_region(ctx, lines, cursor, len(lines), ANCHOR_NONE)
    return True


def _emit_regex(ctx: Ctx, lines: list[str]) -> None:
    starts: list[int] = []
    for i, line in enumerate(lines):
        if _ANCHOR_RE.match(line):
            floor = starts[-1] + 1 if starts else 0
            starts.append(_attach_preamble(lines, i, floor))
    if not starts:
        _emit_region(ctx, lines, 0, len(lines), ANCHOR_NONE)
        return
    if starts[0] > 0:
        _emit_region(ctx, lines, 0, starts[0], ANCHOR_NONE)
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        _emit_definition(ctx, lines, start, end, [])


def extract_code(ctx: Ctx) -> None:
    text = read_text(ctx)
    lines = text.split("\n")
    is_python = ctx.ext in _PY_EXTS or (lines and lines[0].startswith("#!") and "python" in lines[0])
    if is_python and _emit_python(ctx, text, lines):
        return
    _emit_regex(ctx, lines)
