"""``find_files``: files, folders and projects by what they are.

Search answers "which passages say X"; the catalog answers "which files or
folders are X" — the report in MKT-report, last week's photos, the Python
robot project. It ranks whole files and folders, not passages:

- by **name and path** (always available, even with no index features), and
- by their **cards** — the one chunk per file (name, path, type, dates,
  author, content head) and per project folder (languages, dependencies,
  README, activity range) that the sync engine keeps for exactly this — by
  keyword and by vector;
- for files, also by **body keyword hits**, at a lower weight, so a file whose
  card says little is still found by what is inside it.

Without a query it lists what the filters select, sorted, and can aggregate
the selection (by type, folder, month or extension) — the answer to "how many
spreadsheets are in MKT-report" without reading any of them.

A folder's dates are its activity: the range of modification times of the
files beneath it, plus its last git commit, so "the project I worked on last
year" is a folder whose activity overlaps last year.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.documents import types as t
from app.documents.query import filters as F
from app.documents.query.fusion import W_LEXICAL, W_LEXICAL_IDS, W_PHRASE, W_VECTOR, RankedList, rrf
from app.documents.query.lexical import search_lexical
from app.documents.query.terms import analyze
from app.documents.query.vector import search_vector
from app.documents.textnorm import fold

KINDS = ("file", "folder", "project")
SORTS = ("relevance", "newest", "oldest", "name", "largest", "smallest")
AGGREGATES = ("by_type", "by_folder", "by_month", "by_extension")

W_NAME = 1.2
W_BODY = 0.7
MAX_LIMIT = 100
_AGG_ROWS = 30


@dataclass
class FindItem:
    kind: str                           # file | folder
    row: dict[str, Any]
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    date: tuple[str, float] | None = None
    # A folder's activity (earliest, latest modification beneath it).
    activity: tuple[float, float] | None = None


@dataclass
class FindOutcome:
    query: str
    kind: str
    mode: str
    mode_reason: str | None
    overview: dict[str, int]
    filters: list[str]
    relaxed: list[str]
    notes: list[str]
    items: list[FindItem]               # this page
    total: int
    page: int
    limit: int
    sort: str
    aggregate: dict[str, Any] | None
    tz_name: str
    tz: Any = None
    date_filtered: bool = False


def _name_ranked(rows: list[dict[str, Any]], tokens: tuple[str, ...]) -> list[int]:
    """Ids ranked by how many query tokens their name (twice) and path
    contain — the plainest signal and the one that always works."""
    folded = [fold(tok) for tok in tokens]
    scored: list[tuple[float, int]] = []
    for r in rows:
        name = fold(r.get("name") or "")
        path = fold(r.get("rel_path") or "")
        s = sum(2.0 for tok in folded if tok in name) + sum(1.0 for tok in folded if tok in path)
        if s:
            scored.append((s, int(r["id"])))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _file_rows_for_scope(engine: Any, scope: F.Scope, columns: str) -> list[dict[str, Any]]:
    db = engine.db
    if scope.file_ids is None:
        rows = db.read_sql(f"SELECT {columns} FROM files WHERE {F.VISIBLE_SQL}", ())
    else:
        rows = []
        ids = scope.file_ids
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            rows += db.read_sql(f"SELECT {columns} FROM files WHERE id IN ({','.join('?' * len(batch))})", batch)
    return [r for r in rows if scope.accepts_file(r)]


def _sort_files(items: list[FindItem], sort: str) -> None:
    def when(it: FindItem) -> float:
        return float(it.date[1]) if it.date else float(it.row.get("mtime") or 0)

    if sort == "relevance":
        items.sort(key=lambda it: (-it.score, -when(it)))
    elif sort == "newest":
        items.sort(key=lambda it: -when(it))
    elif sort == "oldest":
        items.sort(key=when)
    elif sort == "name":
        items.sort(key=lambda it: fold(it.row.get("name") or ""))
    elif sort == "largest":
        items.sort(key=lambda it: -(it.row.get("size") or 0))
    elif sort == "smallest":
        items.sort(key=lambda it: it.row.get("size") or 0)


def _modes(lex_ok: bool, vec: Any, engine: Any) -> tuple[str, str | None]:
    if lex_ok and vec.available:
        cov = engine.coverage(vec.gen)
        if cov is not None and cov < 0.999:
            return "hybrid_partial", f"vectors cover {int(cov * 100)}% of the index so far"
        return "hybrid", None
    if lex_ok:
        return "lexical_only", vec.reason
    if vec.available:
        return "vector_only", "no full-text index"
    return "catalog_only", "names and paths only: " + "; ".join(r for r in (vec.reason,) if r)


# ── files ──────────────────────────────────────────────────────────────────


def _find_files(
    engine: Any, query: str, f: F.Filters, scope: F.Scope, sort: str,
) -> tuple[list[FindItem], str, str | None]:
    db = engine.db
    terms = analyze(query) if query else None
    if terms is None or terms.empty:
        if scope.file_ids is None:
            order = {
                "newest": "mtime DESC", "oldest": "mtime ASC", "name": "name_folded ASC",
                "largest": "size DESC", "smallest": "size ASC", "relevance": "mtime DESC",
            }[sort]
            # A listing, not a search: the first 5000 in the requested order
            # are more than any page will reach.
            rows = db.read_sql(
                f"SELECT * FROM files WHERE {F.VISIBLE_SQL} ORDER BY {order}, id LIMIT 5000", (), table="files")
            rows = [r for r in rows if scope.accepts_file(r)]
        else:
            rows = list(engine.files(scope.file_ids[:20000]).values())
        items = [FindItem("file", r, date=F.matched_date(r, f.date_field, scope.window, scope.first_seen_cutoff))
                 for r in rows]
        _sort_files(items, "newest" if sort == "relevance" else sort)
        return items, "catalog", None

    accept = engine.acceptor(scope)
    lists: list[RankedList] = []

    # Names and paths: a LIKE per token over the folded name or the path.
    toks = [fold(x) for x in terms.tokens][:8]
    if toks:
        cond = " OR ".join(["name_folded LIKE ? ESCAPE '\\' OR rel_path LIKE ? ESCAPE '\\'"] * len(toks))
        params: list[Any] = []
        for tok in toks:
            params += [_like(tok), _like(tok)]
        name_rows = db.read_sql(
            f"SELECT id, name, rel_path, status, source FROM files WHERE {F.VISIBLE_SQL} AND ({cond}) LIMIT 2000",
            params)
        name_rows = [r for r in name_rows if scope.accepts_file(r)]
        lists.append(RankedList("name", W_NAME, _name_ranked(name_rows, terms.tokens)))

    def to_files(chunk_ids: list[int]) -> list[int]:
        rows = {int(r["id"]): r for r in db.chunk_rows(chunk_ids)}
        out: list[int] = []
        for cid in chunk_ids:
            r = rows.get(cid)
            if r and r.get("file_id") is not None and accept(r) and int(r["file_id"]) not in out:
                out.append(int(r["file_id"]))
        return out

    w_lex = W_LEXICAL_IDS if terms.has_ids_or_numbers else W_LEXICAL
    cards = search_lexical(db, terms, scope=scope, limit=200, ctype=t.CTYPE_FILE_CARD)
    body = search_lexical(db, terms, scope=scope, limit=200)
    if cards.available:
        lists.append(RankedList("lexical", w_lex, to_files(cards.main)))
        if cards.phrase:
            lists.append(RankedList("phrase", W_PHRASE, to_files(cards.phrase)))
    if body.available:
        lists.append(RankedList("body", W_BODY, to_files(body.main + body.phrase)))
    handles = engine.vector_handles()
    vec = search_vector(db, terms.raw, scope=scope, k=100, accept=accept, ctypes=[t.CTYPE_FILE_CARD],
                        handles=handles) if handles is not None else _no_vec(engine)
    if vec.available:
        lists.append(RankedList("vector", W_VECTOR, to_files([cid for cid, _ in vec.hits])))

    fused = rrf(lists)
    rows = engine.files(list(fused))
    items: list[FindItem] = []
    for fid, (score, ranks) in fused.items():
        r = rows.get(fid)
        if r is None or not scope.accepts_file(r):
            continue
        factor, reasons = F.soft_boost(r, f, engine.identity)
        items.append(FindItem("file", r, score=score * factor, ranks=ranks, reasons=reasons,
                              date=F.matched_date(r, f.date_field, scope.window, scope.first_seen_cutoff)))
    _sort_files(items, sort)
    mode, reason = _modes(cards.available, vec, engine)
    return items, mode, reason


def _no_vec(engine: Any) -> Any:
    from app.documents.query.vector import VectorResult

    off = engine.snapshot.get("tool_mode") == "lexical_only"
    return VectorResult(available=False, reason="Vector Embedding is off" if off else "Vector Embedding is not ready")


# ── folders and projects ───────────────────────────────────────────────────


def _activity(engine: Any, folders: list[dict[str, Any]]) -> dict[int, tuple[float, float]]:
    """(earliest, latest) modification time of the visible files beneath
    each folder — one pass over the files, attributing each to every
    candidate ancestor."""
    by_path = {(r["source"], r["rel_path"]): int(r["id"]) for r in folders}
    out: dict[int, list[float]] = {}
    for r in engine.db.read_sql(f"SELECT source, rel_path, mtime FROM files WHERE {F.VISIBLE_SQL}", ()):
        ts = r.get("mtime")
        if ts is None:
            continue
        parts = (r["rel_path"] or "").split("/")[:-1]
        for i in range(1, len(parts) + 1):
            fid = by_path.get((r["source"], "/".join(parts[:i])))
            if fid is None:
                continue
            span = out.setdefault(fid, [float(ts), float(ts)])
            span[0] = min(span[0], float(ts))
            span[1] = max(span[1], float(ts))
    return {k: (v[0], v[1]) for k, v in out.items()}


def _find_folders(engine: Any, query: str, f: F.Filters, scope: F.Scope, kind: str, sort: str,
                  notes: list[str]) -> tuple[list[FindItem], str, str | None]:
    db = engine.db
    folders = [r for r in engine.folders().values() if (r.get("status") or "live") == "live"]
    if kind == "project":
        folders = [r for r in folders if r.get("is_project")]
    folders = [r for r in folders if r.get("source") not in scope.hidden_sources
               and (not scope.source or r.get("source") == scope.source)]
    if scope.folder_prefixes:
        folders = [r for r in folders if any(
            r["source"] == src and (r["rel_path"] == p or r["rel_path"].startswith(p + "/"))
            for src, p in scope.folder_prefixes)]
    if _content_filters(f):
        # Type, size, name… filters: a folder qualifies when it holds at
        # least one matching file (anywhere beneath it). Its date is judged
        # by activity below, not by this.
        file_scope = engine.scope(_drop_date(f))
        holders: set[tuple[str, str]] = set()
        for r in _file_rows_for_scope(engine, file_scope, "id, source, rel_path, status"):
            parts = (r["rel_path"] or "").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                holders.add((r["source"], "/".join(parts[:i])))
        folders = [r for r in folders if (r["source"], r["rel_path"]) in holders]

    activity = _activity(engine, folders) if folders else {}
    if scope.window is not None:
        # A folder's date is its activity: the span of its files' changes
        # overlapping the window, or its last commit falling inside it.
        w = scope.window
        kept = []
        for r in folders:
            span = activity.get(int(r["id"]))
            overlap = span is not None and span[0] < w.end and span[1] >= w.start
            commit = w.contains(r.get("git_last_commit_at"))
            if overlap or commit:
                kept.append(r)
        folders = kept

    items_by_id = {int(r["id"]): FindItem("folder", r, activity=activity.get(int(r["id"]))) for r in folders}
    terms = analyze(query) if query else None
    mode, reason = "catalog", None
    if terms is not None and not terms.empty:
        lists = [RankedList("name", W_NAME, _name_ranked(folders, terms.tokens))]
        allowed = set(items_by_id)

        def to_folders(chunk_ids: list[int]) -> list[int]:
            rows = {int(r["id"]): r for r in db.chunk_rows(chunk_ids)}
            out: list[int] = []
            for cid in chunk_ids:
                r = rows.get(cid)
                if r and r.get("file_id") is None and r.get("folder_id") is not None:
                    fid = int(r["folder_id"])
                    if fid in allowed and fid not in out:
                        out.append(fid)
            return out

        w_lex = W_LEXICAL_IDS if terms.has_ids_or_numbers else W_LEXICAL
        cards = search_lexical(db, terms, scope=F.Scope(), limit=200, ctype=t.CTYPE_FOLDER_CARD)
        if cards.available:
            lists.append(RankedList("lexical", w_lex, to_folders(cards.main)))
            if cards.phrase:
                lists.append(RankedList("phrase", W_PHRASE, to_folders(cards.phrase)))
        handles = engine.vector_handles()

        def accept_card(row: dict[str, Any]) -> bool:
            return row.get("file_id") is None and row.get("folder_id") is not None \
                and int(row["folder_id"]) in allowed

        vec = search_vector(db, terms.raw, scope=F.Scope(), k=100, accept=accept_card,
                            ctypes=[t.CTYPE_FOLDER_CARD], handles=handles) if handles is not None else _no_vec(engine)
        if vec.available:
            lists.append(RankedList("vector", W_VECTOR, to_folders([cid for cid, _ in vec.hits])))
        fused = rrf(lists)
        items = []
        for fid, (score, ranks) in fused.items():
            it = items_by_id.get(fid)
            if it is not None:
                it.score, it.ranks = score, ranks
                items.append(it)
        mode, reason = _modes(cards.available, vec, engine)
    else:
        items = list(items_by_id.values())

    def latest(it: FindItem) -> float:
        span = it.activity[1] if it.activity else 0.0
        return max(span, float(it.row.get("git_last_commit_at") or 0))

    if sort == "relevance" and terms is not None and not terms.empty:
        items.sort(key=lambda it: (-it.score, -latest(it)))
    elif sort == "oldest":
        items.sort(key=latest)
    elif sort == "name":
        items.sort(key=lambda it: fold(it.row.get("name") or ""))
    elif sort in ("largest", "smallest"):
        items.sort(key=lambda it: (it.row.get("file_count") or 0) * (-1 if sort == "largest" else 1))
    else:
        items.sort(key=lambda it: -latest(it))
    if not folders and kind == "project":
        notes.append("No project folder matches. Projects are folders with a README, a manifest "
                     "(pyproject.toml, package.json, …), a .git folder, or several source files.")
    return items, mode, reason


def _drop_date(f: F.Filters) -> F.Filters:
    return dataclasses.replace(f, date_from=None, date_to=None)


def _content_filters(f: F.Filters) -> bool:
    """File-level filters other than the date window."""
    return bool(f.types or f.extensions or f.name_query or f.path_globs or f.size_min is not None
                or f.size_max is not None or f.has_gps is not None or f.file_ids)


# ── aggregates ─────────────────────────────────────────────────────────────


def _aggregate(engine: Any, scope: F.Scope, by: str) -> dict[str, Any]:
    rows = _file_rows_for_scope(engine, scope, "id, kind, ext, mtime, folder_id, size, status, source")
    counts: Counter[str] = Counter()
    sizes: defaultdict[str, int] = defaultdict(int)
    folders = engine.folders() if by == "by_folder" else {}
    for r in rows:
        if by == "by_type":
            key = F.type_of_kind(r.get("kind"))
        elif by == "by_extension":
            key = r.get("ext") or "(none)"
        elif by == "by_month":
            ts = r.get("mtime")
            key = _dt.datetime.fromtimestamp(float(ts), engine.tz).strftime("%Y-%m") if ts else "(unknown)"
        else:
            folder = folders.get(int(r["folder_id"])) if r.get("folder_id") is not None else None
            key = folder["rel_path"] if folder else "(top level)"
        counts[key] += 1
        sizes[key] += int(r.get("size") or 0)
    ordered = sorted(counts, reverse=True) if by == "by_month" else [k for k, _ in counts.most_common()]
    shown = ordered[:_AGG_ROWS]
    return {
        "by": by,
        "files": len(rows),
        "rows": [{"key": k, "files": counts[k], "bytes": sizes[k]} for k in shown],
        "more": max(0, len(ordered) - len(shown)),
    }


# ── entry point ────────────────────────────────────────────────────────────


def find(
    engine: Any,
    query: str | None = None,
    *,
    kind: str = "file",
    filters: Any = None,
    sort: str | None = None,
    limit: int = 20,
    aggregate: str | None = None,
    page: int = 1,
) -> FindOutcome:
    from app.documents.query.engine import RELAX_STEPS, status_notes

    if kind not in KINDS:
        raise F.FilterError(f"kind must be one of {', '.join(KINDS)}")
    if aggregate is not None and aggregate not in AGGREGATES:
        raise F.FilterError(f"aggregate must be one of {', '.join(AGGREGATES)}")
    f = filters if isinstance(filters, F.Filters) else F.parse_filters(filters)
    query = (query or "").strip()
    sort = sort or ("relevance" if query else "newest")
    if sort not in SORTS:
        raise F.FilterError(f"sort must be one of {', '.join(SORTS)}")
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    page = max(1, int(page or 1))
    notes = status_notes(engine.snapshot)
    relaxed: list[str] = []

    def run(scope: F.Scope) -> tuple[list[FindItem], str, str | None]:
        # A date window holding no file changes can still overlap a folder's
        # activity (changes before and after it), so only files stop here.
        if scope.unresolved or (kind == "file" and scope.empty):
            return [], "catalog", None
        if kind == "file":
            return _find_files(engine, query, f, scope, sort)
        return _find_folders(engine, query, f, scope, kind, sort, notes)

    def supported(found: list[FindItem]) -> bool:
        # As in search: nearest-neighbour card hits alone are no evidence
        # that the window matched; a name or keyword match is.
        return bool(found) and (not query or any(set(it.ranks) - {"vector"} for it in found))

    scope = engine.scope(f)
    items, mode, reason = run(scope)
    if f.has_date and not scope.unresolved and not supported(items):
        first = scope.window.label if scope.window else ""
        kept = (items, mode, reason, scope, "") if items else None
        for widen, drop in RELAX_STEPS:
            wider = engine.scope(f, widen_days=widen, ignore_date=drop)
            found = run(wider)
            step = "dropped the date filter" if drop else f"widened to ±{widen} days ({wider.window.label})"
            if supported(found[0]):
                (items, mode, reason), scope = found, wider
                relaxed.append(f"nothing matched {first}; {step}")
                break
            if found[0] and kept is None:
                kept = (*found, wider, step)
        else:
            if kept is None:
                relaxed.append(f"nothing matched {first}, even without the date filter")
            else:
                items, mode, reason, scope, step = kept
                relaxed.append(f"nothing matched {first}; {step} (closest matches only)" if step else
                               f"no name or keyword match in {first}, even with wider dates; "
                               "showing the closest matches in that window")
    notes += scope.notes

    agg = _aggregate(engine, scope, aggregate) if aggregate and kind == "file" else None
    if aggregate and kind != "file":
        notes.append("aggregate applies to kind=file only")
    total = len(items)
    start = (page - 1) * limit
    shown = items[start:start + limit]
    folder_ids = [int(it.row["id"]) for it in shown if it.kind == "folder"]
    if folder_ids:
        # The brief rows ranked them; the shown ones get their project
        # details (languages, markers) for the listing.
        full = engine.db.folders_by_ids(folder_ids)
        for it in shown:
            if it.kind == "folder" and int(it.row["id"]) in full:
                it.row = {**it.row, **full[int(it.row["id"])]}
    return FindOutcome(
        query=query, kind=kind, mode=mode, mode_reason=reason, overview=engine.overview(),
        filters=f.describe(engine.tz_name), relaxed=relaxed, notes=notes, items=shown,
        total=total, page=page, limit=limit, sort=sort, aggregate=agg, tz_name=engine.tz_name, tz=engine.tz,
        date_filtered=scope.window is not None,
    )


__all__ = ["AGGREGATES", "FindItem", "FindOutcome", "KINDS", "SORTS", "find"]
