"""The compiled table as files the user can open: CSV and Markdown.

A compile job's table is the deliverable, and a table of a few hundred rows
does not belong in a chat message. So the job also writes it out — a CSV for
a spreadsheet, a Markdown file for reading — and returns them as ``_files``
entries the chat shows as attachments.

Both go under the job's artifacts folder in Cremind's system folder
(``<SYSTEM_DIR>/<profile>/exports/research/<job_id>/``), never inside the
user's indexed folder: research reads the user's files, it does not add to
them (a file written there would also be indexed and cited back).

Every row keeps its sources — the citation tokens and the files they point
into — so a number in the spreadsheet can be traced to the passage it came
from. Cell text comes from documents, so the CSV defuses spreadsheet
formulas ("=HYPERLINK(…)" in a document must not run when the CSV opens).
"""

from __future__ import annotations

import csv
import io
import os
import re
from typing import Any

from app.userdocs.cite import parse_tokens
from app.userdocs.research.types import Cell, CompiledTable

SOURCES_COLUMN = "sources"
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
# A plain number ("-1,200.5", "+3%") may start with a sign; anything else
# starting with one is treated as a formula.
_PLAIN_NUMBER_RE = re.compile(r"[-+]?\d[\d.,\s\u00a0\u202f]*%?")


class ArtifactPathError(ValueError):
    """The artifacts folder is inside the user's own folder."""


def is_inside(path: str, root: str | None) -> bool:
    """Whether ``path`` is ``root`` or somewhere under it (after resolving
    links; case-insensitively where the filesystem is)."""
    if not root:
        return False
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def row_tokens(row: dict[str, Cell]) -> list[str]:
    """Every evidence token of a row's cells, in first-seen order."""
    out: list[str] = []
    for cell in row.values():
        for tok in cell.tokens:
            if tok not in out:
                out.append(tok)
    return out


def sources_text(tokens: list[str], paths: dict[str, str] | None) -> str:
    """``[ud:…#…] MKT-report/q1.md; [ud:…#…] …`` — each token with the file
    it points into (``paths`` maps a file's cite id to its rel_path)."""
    parts: list[str] = []
    for tok in tokens:
        parsed = parse_tokens(tok)
        rel = (paths or {}).get(parsed[0]["cite_id"]) if parsed else None
        parts.append(f"{tok} {rel}" if rel else tok)
    return "; ".join(parts)


def _sources_header(columns: list[str]) -> str:
    header = SOURCES_COLUMN
    while header in columns:
        header += "_"
    return header


def _csv_safe(value: str) -> str:
    if value and value.startswith(_FORMULA_LEAD) and not _PLAIN_NUMBER_RE.fullmatch(value.strip()):
        return "'" + value
    return value


def table_csv(table: CompiledTable, paths: dict[str, str] | None = None) -> str:
    """One column per table column, then a sources column."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([_csv_safe(c) for c in table.columns] + [_sources_header(table.columns)])
    for row in table.rows:
        cells = [_csv_safe((row.get(c) or Cell("")).value) for c in table.columns]
        w.writerow(cells + [sources_text(row_tokens(row), paths)])
    return buf.getvalue()


def _md(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def table_markdown(
    table: CompiledTable,
    question: str,
    paths: dict[str, str] | None = None,
    *,
    title: str | None = None,
    notes: list[str] | None = None,
) -> str:
    """The table as a Markdown document: the question, the table with a
    sources column, and every conflict with each value's sources."""
    head = _sources_header(table.columns)
    lines = [f"# {_md(title or 'Compiled table')}", "", f"Question: {_md(question)}", ""]
    for n in notes or []:
        lines.append(f"> {_md(n)}")
    if notes:
        lines.append("")
    if not table.columns:
        lines.append("_No table: nothing was extracted._")
        return "\n".join(lines) + "\n"
    lines.append("| " + " | ".join(_md(c) for c in table.columns + [head]) + " |")
    lines.append("|" + "---|" * (len(table.columns) + 1))
    for row in table.rows:
        cells = [_md((row.get(c) or Cell("")).value) for c in table.columns]
        lines.append("| " + " | ".join(cells + [_md(sources_text(row_tokens(row), paths))]) + " |")
    if not table.rows:
        lines.append("")
        lines.append("_No rows: nothing in the files matched the table._")
    if table.conflicts:
        lines += ["", "## Conflicts", "",
                  "The sources disagree on these values; the table shows the first, all are listed here.", ""]
        for c in table.conflicts:
            vals = " · ".join(f"\"{_md(v.value)}\" ({_md(sources_text(v.tokens, paths))})" for v in c.values)
            lines.append(f"- **{_md(c.key)}**: {vals}")
    return "\n".join(lines) + "\n"


def _write(path: str, text: str, encoding: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _entry(path: str, mime: str) -> dict[str, Any]:
    return {
        "uri": os.path.abspath(path).replace(os.sep, "/"),
        "name": os.path.basename(path),
        "mime_type": mime,
        "origin": "created",
    }


def write_table(
    artifacts_dir: str,
    job_id: str,
    question: str,
    table: CompiledTable,
    *,
    paths: dict[str, str] | None = None,
    title: str | None = None,
    notes: list[str] | None = None,
    forbid_under: str | None = None,
) -> list[dict[str, Any]]:
    """Write ``compiled-<job_id>.csv`` and ``.md`` into ``artifacts_dir``
    and return their ``_files`` entries. Blocking (run it in a thread).

    Raises :class:`ArtifactPathError` when ``artifacts_dir`` is inside
    ``forbid_under`` (the user's folder). A re-run of the same job replaces
    its files rather than adding more."""
    if forbid_under and is_inside(artifacts_dir, forbid_under):
        raise ArtifactPathError(f"{artifacts_dir} is inside the user's folder")
    os.makedirs(artifacts_dir, exist_ok=True)
    base = os.path.join(artifacts_dir, f"compiled-{job_id}")
    # utf-8-sig: Excel opens a BOM-less CSV as the ANSI code page, which
    # garbles Vietnamese.
    _write(base + ".csv", table_csv(table, paths), "utf-8-sig")
    _write(base + ".md", table_markdown(table, question, paths, title=title, notes=notes), "utf-8")
    return [_entry(base + ".csv", "text/csv"), _entry(base + ".md", "text/markdown")]


__all__ = [
    "ArtifactPathError", "SOURCES_COLUMN", "is_inside", "row_tokens", "sources_text", "table_csv",
    "table_markdown", "write_table",
]
