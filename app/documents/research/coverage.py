"""The coverage table: every file in scope, and whether it can be read.

"Coverage first" is the research rule: before concluding anything, the job
lists every file the question covers and says, per file, whether its content
was read — and if not, why (an encrypted PDF, a photo waiting for the vision
model, a legacy format, a file still being indexed…). An answer built on a
folder is only as good as the files in it that were actually read, and the
user must see the ones that were not.

:func:`resolve_scope` also catches an ambiguous scope: "ABC" matching two
folders is a question for the user, not a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.documents import types as t
from app.documents.query import filters as F
from app.documents.research.context import ResearchContext
from app.documents.research.types import (
    READ_FULL,
    READ_NONE,
    READ_PARTIAL,
    ROLE_PRIMARY,
    Clarification,
    CoverageRow,
)

# Index statuses whose content is (at least partly) in the index.
_READABLE_STATUSES = frozenset({"indexed"})
_METADATA_REASONS = {
    "legacy_format": "legacy_format",
    "encrypted": "encrypted",
    "too_large": "too_large",
    "placeholder": "placeholder",
}
MAX_SCOPE_FILES = 5000


@dataclass
class ScopeResult:
    files: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Set when the scope is ambiguous or matched nothing: what to ask.
    clarification: Clarification | None = None
    truncated: bool = False


def _ambiguity(engine: Any, names: list[str]) -> Clarification | None:
    folders = list(engine.folders().values())
    for raw in names:
        rows, _notes, unresolved = F.resolve_folder_names(folders, [raw])
        if unresolved:
            return Clarification(
                kind="scope",
                question=f"No indexed folder matches {raw!r}. Which folder did you mean?",
                candidates=[{"rel_path": n} for n in _close_names(folders, raw)],
                answer_keys={"scope_folder": "the folder path to use instead"},
            )
        if len(rows) > 1:
            return Clarification(
                kind="scope",
                question=f"{raw!r} matches {len(rows)} folders. Which one?",
                candidates=[{"fid": r.get("cite_id"), "rel_path": r.get("rel_path"),
                             "files": r.get("file_count")} for r in rows[:12]],
                answer_keys={"scope_folder": "the rel_path of the folder to use"},
            )
    return None


def _close_names(folders: list[dict[str, Any]], raw: str) -> list[str]:
    import difflib

    names = sorted({f.get("rel_path") or "" for f in folders if f.get("rel_path")})
    return difflib.get_close_matches(raw, names, n=6, cutoff=0.4)


def _resolve_sync(engine: Any, filters: dict[str, Any] | None) -> ScopeResult:
    f = F.parse_filters(filters or {})
    if f.folders:
        clar = _ambiguity(engine, list(f.folders))
        if clar is not None:
            return ScopeResult(clarification=clar)
    scope = engine.scope(f)
    if scope.empty:
        return ScopeResult(notes=list(scope.notes) or ["the scope matched no files"])
    db = engine.db
    cols = "*"
    if scope.file_ids is None:
        rows = db.read_sql(f"SELECT {cols} FROM files WHERE {F.VISIBLE_SQL} ORDER BY rel_path", (), table="files")
    else:
        rows = []
        ids = scope.file_ids
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            rows += db.read_sql(f"SELECT {cols} FROM files WHERE id IN ({','.join('?' * len(batch))})", batch,
                                table="files")
        rows.sort(key=lambda r: r.get("rel_path") or "")
    rows = [r for r in rows if scope.accepts_file(r)]
    truncated = len(rows) > MAX_SCOPE_FILES
    return ScopeResult(files=rows[:MAX_SCOPE_FILES], notes=list(scope.notes), truncated=truncated)


async def resolve_scope(
    ctx: ResearchContext, filters: dict[str, Any] | None, *, answer_key: str = "scope_folder",
) -> ScopeResult:
    """Every visible file the filters select (all files when ``filters`` is
    empty), sorted by path; or what to ask when a folder name is ambiguous or
    unknown. The question asked names ``answer_key``, and an answer under that
    key in ``ctx.answers`` replaces the folder filter — one key per scope, so
    answering which case folder is meant never redirects the reference scope
    (``reference_folder``)."""
    chosen = ctx.answers.get(answer_key)
    if chosen and filters and filters.get("folder"):
        filters = {**filters, "folder": [str(chosen)]}
    result = await ctx.io(_resolve_sync, ctx.engine, filters)
    if result.clarification is not None and answer_key != "scope_folder":
        result.clarification.answer_keys = {
            answer_key: v for v in result.clarification.answer_keys.values()
        }
    return result


def unread_reason(row: dict[str, Any]) -> str | None:
    """Why this file's content cannot be read from the index, or None when
    it can (fully or partly)."""
    status = row.get("status") or ""
    reason = (row.get("status_reason") or "").strip()
    if status in ("dirty", "deferred"):
        return "not_indexed_yet"
    if status == "awaiting_extractor":
        return "awaiting_extractor"
    if status == "error":
        return "error"
    if status == "metadata_only":
        return _METADATA_REASONS.get(reason, "metadata_only")
    if status not in _READABLE_STATUSES:
        return "not_indexed_yet"
    if row.get("kind") == t.KIND_IMAGE and row.get("caption_state") not in (None, "done"):
        state = row.get("caption_state")
        return state if state in ("awaiting_vision", "awaiting_consent", "over_cap") else "metadata_only"
    return None


def is_partial(row: dict[str, Any]) -> bool:
    """Indexed, but only in part (the head of a file over a size limit)."""
    return (row.get("status_reason") or "").startswith("partial")


def coverage_row(row: dict[str, Any], role: str = ROLE_PRIMARY, *, chunks_total: int = 0,
                 chunks_read: int = 0) -> CoverageRow:
    """A coverage row for ``row``. ``read`` is ``read`` when every content
    chunk was read and the file is not itself partial, ``partial`` when some
    were (or the file is), ``unread`` otherwise."""
    reason = unread_reason(row)
    if reason is None and chunks_total and chunks_read >= chunks_total:
        read = READ_PARTIAL if is_partial(row) else READ_FULL
        if is_partial(row):
            reason = "too_large"
    elif reason is None and chunks_read > 0:
        read = READ_PARTIAL
    else:
        read = READ_NONE
    return CoverageRow(
        fid=row.get("cite_id") or "", rel_path=row.get("rel_path") or "", kind=row.get("kind") or "",
        role=role, read=read, reason=reason, chunks_read=chunks_read, chunks_total=chunks_total,
    )


def unread_rows(rows: list[CoverageRow]) -> list[CoverageRow]:
    return [r for r in rows if r.read != READ_FULL]


__all__ = ["MAX_SCOPE_FILES", "ScopeResult", "coverage_row", "is_partial", "resolve_scope", "unread_reason",
           "unread_rows"]
