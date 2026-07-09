"""Convert a Google Docs `documents.get` resource to markdown or plain text.

Covered: paragraphs, headings (HEADING_1-6/TITLE/SUBTITLE), ordered/unordered
lists (with nesting), links, tables (GitHub pipe tables), horizontal rules, and
inline objects (as an [image] placeholder). NOT converted (v1 limitations):
character styles (bold/italic), and headers/footers/footnotes.
"""
from __future__ import annotations

from typing import Any

# glyphType values that denote an ordered (numbered) list.
_ORDERED_GLYPHS = {
    "DECIMAL", "ZERO_DECIMAL", "ALPHA", "UPPER_ALPHA", "ROMAN", "UPPER_ROMAN",
}

_HEADING_PREFIX = {
    "TITLE": "# ",
    "SUBTITLE": "## ",
    "HEADING_1": "# ",
    "HEADING_2": "## ",
    "HEADING_3": "### ",
    "HEADING_4": "#### ",
    "HEADING_5": "##### ",
    "HEADING_6": "###### ",
}


def _inline_text(paragraph: dict[str, Any], *, markdown: bool) -> str:
    out: list[str] = []
    for el in paragraph.get("elements", []) or []:
        if "textRun" in el:
            run = el["textRun"]
            text = run.get("content", "")
            link = ((run.get("textStyle", {}) or {}).get("link", {}) or {}).get("url")
            if markdown and link and text.strip():
                # Keep any trailing newline outside the link markup.
                stripped = text.rstrip("\n")
                trailing = text[len(stripped):]
                out.append(f"[{stripped}]({link}){trailing}")
            else:
                out.append(text)
        elif "inlineObjectElement" in el and markdown:
            out.append("[image]")
    return "".join(out).rstrip("\n")


def _list_marker(doc: dict[str, Any], paragraph: dict[str, Any]) -> tuple[str, int] | None:
    bullet = paragraph.get("bullet")
    if not bullet:
        return None
    list_id = bullet.get("listId")
    level = bullet.get("nestingLevel", 0) or 0
    glyph_type = ""
    lst = (doc.get("lists", {}) or {}).get(list_id, {}) or {}
    levels = (lst.get("listProperties", {}) or {}).get("nestingLevels", []) or []
    if 0 <= level < len(levels):
        glyph_type = levels[level].get("glyphType", "") or ""
    marker = "1." if glyph_type in _ORDERED_GLYPHS else "-"
    return marker, level


def _paragraph_markdown(doc: dict[str, Any], paragraph: dict[str, Any]) -> str:
    text = _inline_text(paragraph, markdown=True)
    marker = _list_marker(doc, paragraph)
    if marker is not None:
        prefix, level = marker
        return f"{'  ' * level}{prefix} {text}"
    style = (paragraph.get("paragraphStyle", {}) or {}).get("namedStyleType", "")
    if style in _HEADING_PREFIX and text.strip():
        return f"{_HEADING_PREFIX[style]}{text}"
    return text


def _has_horizontal_rule(paragraph: dict[str, Any]) -> bool:
    return any("horizontalRule" in el for el in paragraph.get("elements", []) or [])


def _cell_text(doc: dict[str, Any], cell: dict[str, Any]) -> str:
    parts: list[str] = []
    for el in cell.get("content", []) or []:
        para = el.get("paragraph")
        if para:
            parts.append(_inline_text(para, markdown=True))
    # Flatten inner newlines so the cell stays on one table row.
    return " ".join(p for p in " ".join(parts).split("\n") if p is not None).strip()


def _table_markdown(doc: dict[str, Any], table: dict[str, Any]) -> list[str]:
    rows = table.get("tableRows", []) or []
    if not rows:
        return []
    grid: list[list[str]] = []
    for row in rows:
        grid.append([_cell_text(doc, c) for c in row.get("tableCells", []) or []])
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in grid[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in grid[1:]:
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
    return lines


def to_markdown(doc: dict[str, Any]) -> str:
    body = (doc.get("body", {}) or {}).get("content", []) or []
    out: list[str] = []
    for el in body:
        if "paragraph" in el:
            para = el["paragraph"]
            if _has_horizontal_rule(para):
                out.append("---")
                continue
            line = _paragraph_markdown(doc, para)
            out.append(line)
        elif "table" in el:
            out.extend(_table_markdown(doc, el["table"]))
            out.append("")
    # Collapse 3+ blank lines to a single blank line; trim leading/trailing blanks.
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"


def to_text(doc: dict[str, Any]) -> str:
    body = (doc.get("body", {}) or {}).get("content", []) or []
    out: list[str] = []
    for el in body:
        if "paragraph" in el:
            out.append(_inline_text(el["paragraph"], markdown=False))
        elif "table" in el:
            for row in el["table"].get("tableRows", []) or []:
                cells = [_cell_text(doc, c) for c in row.get("tableCells", []) or []]
                out.append("\t".join(cells))
    text = "\n".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"
