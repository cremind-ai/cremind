"""One filter schema for every Documentation Search leaf, and what it selects.

The agent passes the same ``filters`` object to ``find_files``, ``search`` and
``read``, so a filter means one thing everywhere. Hard filters (folder, type,
source, date window, size, GPS, explicit files) become SQL over the ``files``
table and produce the *scope*: the file ids a hit may come from. Soft filters
(author, photographer, image origin) never remove a hit; they only lift the
ones that match, because the metadata they rely on is often missing (a PDF
with no author field, a photo whose EXIF was stripped) and "no author field"
must not read as "not written by me".

Dates are the subtle part. "The doc I wrote two days ago" can be recorded in
five places — the document's own creation date, the file's birth time, its
modification time, the photo's EXIF time, or when Cremind first saw it — and
which one is right depends on how the file got there. ``date_field="any"``
therefore matches the union, and every hit reports which date matched, so the
agent (and the user) can see why a file qualified. ``first_seen_at`` only
counts for files that appeared after the first sync: before that it is the day
the index was built, which says nothing about the file.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.documents import types as t
from app.documents.textnorm import fold

# ── Types ──────────────────────────────────────────────────────────────────

_WORD = frozenset({t.KIND_DOCX, t.KIND_DOC, t.KIND_ODT, t.KIND_RTF})
_SHEET = frozenset({t.KIND_XLSX, t.KIND_XLS, t.KIND_ODS, t.KIND_CSV})
_SLIDES = frozenset({t.KIND_PPTX, t.KIND_PPT, t.KIND_ODP})
_TEXT = frozenset({t.KIND_TEXT, t.KIND_MARKDOWN})

# What a person means by each type word, in file kinds (what the magic bytes
# said, not the extension). "document" is the broad reading of "a doc I
# wrote": prose in any container, slides included, spreadsheets not.
TYPE_KINDS: dict[str, frozenset[str]] = {
    "document": frozenset({t.KIND_PDF, t.KIND_EPUB, t.KIND_HTML, t.KIND_EML, t.KIND_MSG})
    | _WORD | _TEXT | _SLIDES,
    "pdf": frozenset({t.KIND_PDF}),
    "word": _WORD,
    "spreadsheet": _SHEET,
    "presentation": _SLIDES,
    "text": _TEXT,
    "code": frozenset({t.KIND_CODE, t.KIND_JSON, t.KIND_XML}),
    "image": frozenset({t.KIND_IMAGE}),
    "audio": frozenset({t.KIND_AUDIO}),
    "video": frozenset({t.KIND_VIDEO}),
    "archive": frozenset({t.KIND_ARCHIVE}),
    "executable": frozenset({t.KIND_EXECUTABLE, t.KIND_BUNDLE}),
    "other": frozenset({t.KIND_OTHER, t.KIND_DATABASE, t.KIND_FONT, t.KIND_ENCRYPTED}),
}
TYPE_NAMES = tuple(TYPE_KINDS)


def type_of_kind(kind: str | None) -> str:
    """The narrowest type word for a file kind (for display and by_type)."""
    for name in ("pdf", "word", "spreadsheet", "presentation", "text", "code", "image",
                 "audio", "video", "archive", "executable"):
        if kind in TYPE_KINDS[name]:
            return name
    if kind in TYPE_KINDS["document"]:
        return "document"
    return "other"


DATE_FIELDS = ("any", "modified", "created", "taken")
SOURCES = ("all", "local", "drive")
IMAGE_ORIGINS = ("camera", "screenshot")

# Hidden from every search: files a mass deletion put on hold ('missing' — the
# user has not confirmed they are gone) and files deleted but still inside the
# move-detection grace period ('tombstone').
VISIBLE_SQL = "status NOT IN ('missing', 'tombstone')"

# Files first seen within this long of the source's first scan were found by
# that initial sync, so their first_seen_at is the day the index was built.
INITIAL_SYNC_WINDOW_S = 6 * 3600.0

# A first-seen cutoff: one epoch for every row, or one per source (the local
# folder and Drive had their first syncs on different days), or None.
Cutoff = float | dict[str, float] | None


def cutoff_for(cutoff: Cutoff, source: str | None) -> float | None:
    """The first-seen cutoff that applies to a row of ``source``."""
    if isinstance(cutoff, dict):
        return cutoff.get(source or "local")
    return cutoff

_SCREENSHOT_RE = re.compile(r"screen\s*shot|screenshot|chup\s*man\s*hinh|anh\s*man\s*hinh|snip", re.I)


class FilterError(ValueError):
    """A filter value the agent must correct (unknown type, bad date, …)."""


# ── Date windows ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DateWindow:
    first: _dt.date
    last: _dt.date          # inclusive
    start: float            # epoch seconds, inclusive
    end: float              # epoch seconds, exclusive

    @property
    def label(self) -> str:
        if self.first == _dt.date.min and self.last == _dt.date.max:
            return "any date"
        if self.first == _dt.date.min:
            return f"until {self.last.isoformat()}"
        if self.last == _dt.date.max:
            return f"since {self.first.isoformat()}"
        if self.first == self.last:
            return self.first.isoformat()
        return f"{self.first.isoformat()}..{self.last.isoformat()}"

    def contains(self, ts: float | None) -> bool:
        return ts is not None and self.start <= float(ts) < self.end


def _parse_day(value: Any, *, end: bool) -> _dt.date:
    """``YYYY-MM-DD`` (a full ISO datetime is cut to its date), ``YYYY-MM`` or
    ``YYYY``. A month or year means its first day as a start and its last
    day as an end, so ``date_from="2025", date_to="2025"`` is all of 2025."""
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[T ].*)?", s)
    if not m:
        raise FilterError(f"not a date: {s!r} (use YYYY-MM-DD, YYYY-MM or YYYY)")
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    day = int(m.group(3)) if m.group(3) else None
    try:
        if month is None:
            return _dt.date(year, 12, 31) if end else _dt.date(year, 1, 1)
        if day is None:
            if not end:
                return _dt.date(year, month, 1)
            nxt = _dt.date(year + (month == 12), month % 12 + 1, 1)
            return nxt - _dt.timedelta(days=1)
        return _dt.date(year, month, day)
    except ValueError:
        raise FilterError(f"not a valid date: {s!r}") from None


def _day_start(d: _dt.date, tz: _dt.tzinfo) -> float:
    return _dt.datetime.combine(d, _dt.time(0), tzinfo=tz).timestamp()


def make_window(first: _dt.date | None, last: _dt.date | None, tz: _dt.tzinfo,
                widen_days: int = 0) -> DateWindow | None:
    """The epoch-second window for [first, last] in ``tz``, widened by
    ``widen_days`` on each side. None when neither bound is set."""
    if first is None and last is None:
        return None
    lo = first or _dt.date.min
    hi = last or _dt.date.max
    if widen_days:
        if lo != _dt.date.min:
            lo = lo - _dt.timedelta(days=widen_days)
        if hi != _dt.date.max:
            hi = hi + _dt.timedelta(days=widen_days)
    start = _day_start(lo, tz) if lo != _dt.date.min else float("-inf")
    end = _day_start(hi + _dt.timedelta(days=1), tz) if hi != _dt.date.max else float("inf")
    return DateWindow(first=lo, last=hi, start=start, end=end)


# Date columns per field, in the order a match is reported (the most specific
# evidence first: a photo's EXIF time beats its copy date).
_FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "taken": ("taken_at",),
    "created": ("doc_created_at", "birthtime"),
    "modified": ("mtime", "drive_modified_by_me_at"),
}
_ANY_ORDER = ("taken_at", "doc_created_at", "birthtime", "mtime", "drive_modified_by_me_at", "first_seen_at")
DATE_LABELS = {
    "taken_at": "taken",
    "doc_created_at": "created (document)",
    "birthtime": "created (file)",
    "mtime": "modified",
    "drive_modified_by_me_at": "modified by you (Drive)",
    "first_seen_at": "first seen",
}


def date_columns(date_field: str) -> tuple[str, ...]:
    if date_field == "any":
        return _ANY_ORDER
    return _FIELD_COLUMNS[date_field]


def matched_date(row: dict[str, Any], date_field: str, window: DateWindow | None,
                 first_seen_cutoff: Cutoff) -> tuple[str, float] | None:
    """Which date of ``row`` put it in ``window`` — ``(column, ts)`` — or,
    with no window, the date to show for it (modified). None when nothing
    matches."""
    if window is None:
        ts = row.get("mtime")
        return ("mtime", float(ts)) if ts else None
    for col in date_columns(date_field):
        ts = row.get(col)
        if col == "first_seen_at" and not _first_seen_counts(ts, cutoff_for(first_seen_cutoff, row.get("source"))):
            continue
        if window.contains(ts):
            return col, float(ts)
    return None


def _first_seen_counts(ts: Any, cutoff: float | None) -> bool:
    return ts is not None and cutoff is not None and float(ts) > cutoff


# ── The filter object ──────────────────────────────────────────────────────


def _str_list(raw: Any, name: str, *, limit: int = 50) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise FilterError(f"{name} must be a list of strings")
    out = [str(x).strip() for x in raw if str(x).strip()]
    return list(dict.fromkeys(out))[:limit]


def _int_or_none(raw: Any, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        raise FilterError(f"{name} must be a number of bytes") from None
    if v < 0:
        raise FilterError(f"{name} must not be negative")
    return v


@dataclass
class Filters:
    folders: list[str] = field(default_factory=list)
    path_globs: list[str] = field(default_factory=list)
    name_query: str | None = None
    types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    source: str = "all"
    date_field: str = "any"
    date_from: _dt.date | None = None
    date_to: _dt.date | None = None
    size_min: int | None = None
    size_max: int | None = None
    author: str | None = None
    taken_by: str | None = None
    image_origin: str | None = None
    has_gps: bool | None = None
    file_ids: list[str] = field(default_factory=list)

    @property
    def has_date(self) -> bool:
        return self.date_from is not None or self.date_to is not None

    @property
    def file_level(self) -> bool:
        """Whether a condition applies to files themselves (type, date,
        size, …) — then a folder's own card cannot qualify as a hit."""
        return bool(
            self.path_globs or self.name_query or self.types or self.extensions or self.has_date
            or self.size_min is not None or self.size_max is not None or self.has_gps
            or self.file_ids
        )

    def describe(self, tz_name: str | None = None) -> list[str]:
        """Human-readable applied filters, for the result header."""
        out: list[str] = []
        if self.folders:
            out.append("folder=" + ", ".join(self.folders))
        if self.path_globs:
            out.append("path=" + ", ".join(self.path_globs))
        if self.name_query:
            out.append(f"name~{self.name_query!r}")
        if self.types:
            out.append("types=" + ",".join(self.types))
        if self.extensions:
            out.append("ext=" + ",".join(self.extensions))
        if self.source != "all":
            out.append(f"source={self.source}")
        if self.has_date:
            lo = self.date_from.isoformat() if self.date_from else "…"
            hi = self.date_to.isoformat() if self.date_to else "…"
            zone = f" ({tz_name})" if tz_name else ""
            out.append(f"date {self.date_field} {lo}..{hi}{zone}")
        if self.size_min is not None or self.size_max is not None:
            out.append(f"size {self.size_min or 0}..{self.size_max if self.size_max is not None else '∞'} B")
        if self.author:
            out.append(f"author={self.author} (soft)")
        if self.taken_by:
            out.append(f"taken_by={self.taken_by} (soft)")
        if self.image_origin:
            out.append(f"image_origin={self.image_origin} (soft)")
        if self.has_gps is not None:
            out.append(f"has_gps={'yes' if self.has_gps else 'no'}")
        if self.file_ids:
            out.append(f"{len(self.file_ids)} explicit file(s)")
        return out


def parse_filters(raw: Any) -> Filters:
    """Validate the agent's ``filters`` object. Unknown keys and bad values
    raise :class:`FilterError` naming the field, so a typo is corrected
    instead of silently widening the search."""
    if raw is None or raw == {}:
        return Filters()
    if not isinstance(raw, dict):
        raise FilterError("filters must be an object")
    known = {
        "folder", "path_glob", "name_query", "types", "extensions", "source", "date_field",
        "date_from", "date_to", "size_min", "size_max", "author", "taken_by", "image_origin",
        "has_gps", "file_ids",
    }
    unknown = sorted(set(raw) - known)
    if unknown:
        raise FilterError(f"unknown filter(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    f = Filters()
    f.folders = _str_list(raw.get("folder"), "folder", limit=20)
    f.path_globs = _str_list(raw.get("path_glob"), "path_glob", limit=20)
    nq = raw.get("name_query")
    f.name_query = str(nq).strip() or None if nq is not None else None
    types = [x.lower() for x in _str_list(raw.get("types"), "types")]
    bad = [x for x in types if x not in TYPE_KINDS]
    if bad:
        raise FilterError(f"unknown type(s): {', '.join(bad)}; use {', '.join(TYPE_NAMES)}")
    f.types = types
    f.extensions = [
        "." + x.lower().lstrip("*").lstrip(".") for x in _str_list(raw.get("extensions"), "extensions")
        if x.lower().lstrip("*").lstrip(".")
    ]
    source = str(raw.get("source") or "all").lower()
    if source not in SOURCES:
        raise FilterError(f"source must be one of {', '.join(SOURCES)}")
    f.source = source
    date_field = str(raw.get("date_field") or "any").lower()
    if date_field not in DATE_FIELDS:
        raise FilterError(f"date_field must be one of {', '.join(DATE_FIELDS)}")
    f.date_field = date_field
    if raw.get("date_from"):
        f.date_from = _parse_day(raw["date_from"], end=False)
    if raw.get("date_to"):
        f.date_to = _parse_day(raw["date_to"], end=True)
    if f.date_from and f.date_to and f.date_from > f.date_to:
        raise FilterError("date_from is after date_to")
    f.size_min = _int_or_none(raw.get("size_min"), "size_min")
    f.size_max = _int_or_none(raw.get("size_max"), "size_max")
    for name in ("author", "taken_by"):
        v = raw.get(name)
        setattr(f, name, str(v).strip() or None if v is not None else None)
    origin = raw.get("image_origin")
    if origin is not None and str(origin).strip():
        origin = str(origin).strip().lower()
        if origin not in IMAGE_ORIGINS:
            raise FilterError(f"image_origin must be one of {', '.join(IMAGE_ORIGINS)}")
        f.image_origin = origin
    if raw.get("has_gps") is not None:
        f.has_gps = bool(raw.get("has_gps"))
    ids = []
    for x in _str_list(raw.get("file_ids"), "file_ids", limit=200):
        m = re.search(r"([0-9a-hjkmnp-tv-z]{8})", x.lower())
        if not m:
            raise FilterError(f"file_ids: {x!r} is not a file id or [doc:…] token")
        ids.append(m.group(1))
    f.file_ids = list(dict.fromkeys(ids))
    return f


# ── Folder names → folder rows ─────────────────────────────────────────────

FUZZY_MIN_RATIO = 0.82


def _norm_name(s: str) -> str:
    """Folded, with separators unified, so "mkt report", "MKT-report" and
    "mkt_report" compare equal."""
    return re.sub(r"[\s_\-]+", " ", fold(s or "").replace("\\", "/")).strip(" /")


def resolve_folder_names(
    folders: list[dict[str, Any]], names: Iterable[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Resolve each folder name the agent gave to folder rows.

    Per name, the first tier that matches wins: the exact folded name (or
    full path); then a path suffix/prefix or a name that starts or ends with
    it ("MKT" → "MKT-report"); then a fuzzy match (difflib ratio ≥ 0.82, for
    typos). Several folders in the winning tier are all kept and reported,
    because "ABC" in two places is a real ambiguity the user should see.

    Returns ``(rows, notes, unresolved names)``; nested matches collapse to
    their outermost folder, since the filter covers subtrees.
    """
    prepared = [(r, _norm_name(r.get("name") or ""), _norm_name(r.get("rel_path") or "")) for r in folders
                if (r.get("status") or "live") == "live"]
    matched: dict[int, dict[str, Any]] = {}
    notes: list[str] = []
    unresolved: list[str] = []
    for raw in names:
        q = _norm_name(raw)
        if not q:
            continue
        tiers = (
            [r for r, n, p in prepared if n == q or p == q],
            [r for r, n, p in prepared
             if p.endswith("/" + q) or p.startswith(q + "/") or n.startswith(q) or n.endswith(q)],
            [r for r, n, _p in prepared if difflib.SequenceMatcher(None, q, n).ratio() >= FUZZY_MIN_RATIO],
        )
        hit = next((tier for tier in tiers if tier), [])
        if not hit:
            unresolved.append(raw)
            close = difflib.get_close_matches(q, sorted({n for _r, n, _p in prepared}), n=5, cutoff=0.5)
            notes.append(f"no folder matches {raw!r}" + (f" (closest: {', '.join(close)})" if close else ""))
            continue
        if len(hit) > 1:
            shown = ", ".join(sorted(r["rel_path"] for r in hit)[:8])
            notes.append(f"folder {raw!r} matched {len(hit)} folders: {shown}")
        elif _norm_name(hit[0].get("name") or "") != q:
            notes.append(f"folder {raw!r} → {hit[0]['rel_path']}")
        for r in hit:
            matched[int(r["id"])] = r
    rows = sorted(matched.values(), key=lambda r: r["rel_path"])
    outer: list[dict[str, Any]] = []
    for r in rows:
        if not any(r["source"] == o["source"] and r["rel_path"].startswith(o["rel_path"] + "/") for o in outer):
            outer.append(r)
    return outer, notes, unresolved


# ── Scope: the file ids a hit may come from ────────────────────────────────


@dataclass
class Scope:
    """What the hard filters select. ``file_ids`` None means "every visible
    file" (no file-level condition), so the lexical and vector stages can run
    unrestricted and only drop invisible files."""

    file_ids: list[int] | None = None
    folder_prefixes: list[tuple[str, str]] = field(default_factory=list)
    cards_allowed: bool = True
    source: str | None = None
    hidden_sources: frozenset[str] = frozenset()
    window: DateWindow | None = None
    first_seen_cutoff: Cutoff = None
    notes: list[str] = field(default_factory=list)
    # A hard filter matched nothing (an unknown folder, a file id that does
    # not exist): the answer is "no results", never "all results".
    empty: bool = False
    # A folder name resolved to no folder at all — no date relaxation can
    # help, unlike a date window that simply held no files.
    unresolved: bool = False
    _ids: set[int] | None = field(default=None, repr=False)

    def set_file_ids(self, ids: list[int]) -> None:
        self.file_ids = list(ids)
        self._ids = set(self.file_ids)
        self.empty = self.empty or not self.file_ids

    @property
    def id_set(self) -> set[int] | None:
        return self._ids

    def accepts_file(self, row: dict[str, Any] | None) -> bool:
        if not row or self.empty:
            return False
        if row.get("status") in ("missing", "tombstone"):
            return False
        if row.get("source") in self.hidden_sources:
            return False
        if self.source and row.get("source") != self.source:
            return False
        return self._ids is None or int(row["id"]) in self._ids

    def accepts_folder(self, row: dict[str, Any] | None) -> bool:
        """A folder card as a hit: only without file-level conditions (a
        folder has no type or date of its own), inside any folder filter."""
        if not row or self.empty or not self.cards_allowed:
            return False
        if row.get("source") in self.hidden_sources or (self.source and row.get("source") != self.source):
            return False
        if not self.folder_prefixes:
            return True
        rel = row.get("rel_path") or ""
        return any(row.get("source") == src and (rel == p or rel.startswith(p + "/"))
                   for src, p in self.folder_prefixes)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def file_conditions(f: Filters, scope: Scope, *, cite_ids: list[str] | None = None) -> tuple[list[str], list[Any]]:
    """SQL conditions over ``files`` for the hard filters (built from
    constants; every value bound). The caller adds folder subtrees."""
    where = [VISIBLE_SQL]
    params: list[Any] = []
    if scope.hidden_sources:
        where.append(f"source NOT IN ({', '.join('?' * len(scope.hidden_sources))})")
        params += sorted(scope.hidden_sources)
    if f.source != "all":
        where.append("source = ?")
        params.append(f.source)
    kinds = sorted(set().union(*(TYPE_KINDS[x] for x in f.types))) if f.types else []
    type_parts = []
    if kinds:
        type_parts.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params += kinds
    if f.extensions:
        type_parts.append(f"ext IN ({', '.join('?' * len(f.extensions))})")
        params += f.extensions
    if type_parts:
        # A type and an extension given together widen each other: "PDFs and
        # .pages files" is two kinds of file, not files that are both.
        where.append("(" + " OR ".join(type_parts) + ")")
    if f.size_min is not None:
        where.append("size >= ?")
        params.append(f.size_min)
    if f.size_max is not None:
        where.append("size <= ?")
        params.append(f.size_max)
    if f.has_gps is not None:
        where.append(("" if f.has_gps else "NOT ") + "COALESCE(exif, '') LIKE '%\"gps\":{%'")
    if f.name_query:
        for tok in fold(f.name_query).split():
            where.append("name_folded LIKE ? ESCAPE '\\'")
            params.append(_like(tok))
    if cite_ids:
        where.append(f"cite_id IN ({', '.join('?' * len(cite_ids))})")
        params += cite_ids
    if scope.window is not None:
        parts = []
        for col in date_columns(f.date_field):
            if col == "first_seen_at":
                cut = scope.first_seen_cutoff
                if isinstance(cut, dict):
                    # Each source counts from its own first sync.
                    for src, value in sorted(cut.items()):
                        if value is None:
                            continue
                        parts.append("(source = ? AND first_seen_at > ? AND first_seen_at >= ? "
                                     "AND first_seen_at < ?)")
                        params += [src, value, _finite(scope.window.start), _finite(scope.window.end)]
                    continue
                if cut is None:
                    continue
                parts.append("(first_seen_at > ? AND first_seen_at >= ? AND first_seen_at < ?)")
                params += [cut, _finite(scope.window.start), _finite(scope.window.end)]
            else:
                parts.append(f"({col} >= ? AND {col} < ?)")
                params += [_finite(scope.window.start), _finite(scope.window.end)]
        where.append("(" + " OR ".join(parts) + ")")
    return where, params


def _finite(x: float) -> float:
    # SQLite binds inf as a REAL that compares correctly, but some builds
    # reject it; the widest representable bounds say the same thing.
    if x == float("inf"):
        return 1e18
    if x == float("-inf"):
        return -1e18
    return x


def folder_conditions(prefixes: list[tuple[str, str]]) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    for src, rel in prefixes:
        parts.append("(source = ? AND (rel_path = ? OR substr(rel_path, 1, ?) = ?))")
        params += [src, rel, len(rel) + 1, rel + "/"]
    return "(" + " OR ".join(parts) + ")", params


def glob_match(rel_path: str, globs: list[str]) -> bool:
    """Case- and accent-insensitive glob over the relative path; a pattern
    without a slash also matches the file name alone ("*.pdf")."""
    p = fold(rel_path)
    name = p.rsplit("/", 1)[-1]
    for g in globs:
        gf = fold(g.replace("\\", "/")).strip("/")
        if fnmatch.fnmatchcase(p, gf) or ("/" not in gf and fnmatch.fnmatchcase(name, gf)):
            return True
        if gf.endswith("/**") and (p == gf[:-3] or p.startswith(gf[:-3] + "/")):
            return True
    return False


# Drive holds that hide Drive's rows: Google no longer lets Cremind read the
# account (access revoked, or the link removed outside Cremind), so what the
# index holds is served to nobody until Google is re-linked or it is purged.
HIDDEN_HOLD_REASONS = frozenset({"auth_revoked", "drive_unlinked"})


def hidden_sources(db: Any) -> frozenset[str]:
    """Sources whose rows must not be served: a Drive whose access was
    revoked or unlinked is on hold until it is re-linked or purged."""
    try:
        drive = db.get_source_state("drive")
    except Exception:  # noqa: BLE001 — a state read must not break search
        return frozenset()
    if (drive or {}).get("state") == "hold" and (drive or {}).get("reason") in HIDDEN_HOLD_REASONS:
        return frozenset({"drive"})
    return frozenset()


def resolve_scope(
    db: Any,
    f: Filters,
    *,
    folders: list[dict[str, Any]] | None,
    tz: _dt.tzinfo,
    widen_days: int = 0,
    first_seen_cutoff: Cutoff = None,
    ignore_date: bool = False,
) -> Scope:
    """Run the hard filters against the index and return the scope.

    ``folders`` is the profile's folder list (only needed when a folder
    filter is set). ``widen_days`` widens the date window (date relaxation);
    ``ignore_date`` drops it altogether.
    """
    scope = Scope(
        hidden_sources=hidden_sources(db),
        source=None if f.source == "all" else f.source,
        first_seen_cutoff=first_seen_cutoff,
        cards_allowed=not f.file_level,
    )
    if f.has_date and not ignore_date:
        scope.window = make_window(f.date_from, f.date_to, tz, widen_days)

    if f.folders:
        rows, notes, unresolved = resolve_folder_names(folders or [], f.folders)
        scope.notes += notes
        if unresolved and not rows:
            scope.empty = scope.unresolved = True
            scope.set_file_ids([])
            return scope
        scope.folder_prefixes = [(r["source"], r["rel_path"]) for r in rows]

    if not (f.file_level or f.folders):
        # Every visible file: no id list. Visibility, the source filter and
        # held sources are checked per hit (Scope.accepts_file).
        return scope

    where, params = file_conditions(f, scope, cite_ids=f.file_ids or None)
    if scope.folder_prefixes:
        clause, extra = folder_conditions(scope.folder_prefixes)
        where.append(clause)
        params += extra
    cols = "id, rel_path" if f.path_globs else "id"
    rows = db.read_sql(f"SELECT {cols} FROM files WHERE {' AND '.join(where)}", params)
    if f.path_globs:
        rows = [r for r in rows if glob_match(r["rel_path"] or "", f.path_globs)]
    scope.set_file_ids([int(r["id"]) for r in rows])
    if f.file_ids and len(scope.file_ids or []) < len(f.file_ids):
        missing = len(f.file_ids) - len(scope.file_ids or [])
        scope.notes.append(f"{missing} of the given file ids are not in this index")
    return scope


# ── Soft filters: identity and image origin ────────────────────────────────


def _contains_any(hay: str, needles: Iterable[str]) -> bool:
    h = fold(hay or "")
    return any(n and fold(n) in h for n in needles)


def soft_boost(row: dict[str, Any], f: Filters, identity: dict[str, Any]) -> tuple[float, list[str]]:
    """A multiplier (≥ 0.8) and the reasons, for the soft filters. A file
    with no author field or no EXIF is left alone (factor 1), never
    demoted: missing metadata is not evidence against it."""
    factor = 1.0
    reasons: list[str] = []
    meta = row.get("doc_meta") or {}
    exif = row.get("exif") or {}
    if f.author:
        names = (identity.get("author_names") or []) + (identity.get("emails") or []) \
            if f.author.lower() == "me" else [f.author]
        who = " ".join(str(meta.get(k) or "") for k in ("author", "last_modified_by"))
        if names and who.strip() and _contains_any(who, names):
            factor *= 1.3
            reasons.append("author matches")
    if f.taken_by:
        if f.taken_by.lower() == "me":
            devices = identity.get("camera_devices") or []
            camera = " ".join(str(exif.get(k) or "") for k in ("make", "model"))
            if devices and camera.strip() and _contains_any(camera, devices):
                factor *= 1.3
                reasons.append("taken with your camera")
            elif not devices and row.get("is_camera_photo"):
                factor *= 1.1
                reasons.append("camera photo")
        else:
            camera = " ".join(str(exif.get(k) or "") for k in ("make", "model"))
            if camera.strip() and _contains_any(camera, [f.taken_by]):
                factor *= 1.3
                reasons.append("camera matches")
    if f.image_origin and row.get("kind") == t.KIND_IMAGE:
        looks_screenshot = bool(_SCREENSHOT_RE.search(fold(row.get("name") or ""))) or \
            bool(_SCREENSHOT_RE.search(str(exif.get("software") or "")))
        if f.image_origin == "camera":
            if row.get("is_camera_photo"):
                factor *= 1.2
                reasons.append("camera photo")
            elif looks_screenshot:
                factor *= 0.8
        elif looks_screenshot:
            factor *= 1.2
            reasons.append("screenshot")
        elif row.get("is_camera_photo"):
            factor *= 0.8
    return factor, reasons


__all__ = [
    "Cutoff",
    "DATE_FIELDS",
    "DATE_LABELS",
    "DateWindow",
    "FilterError",
    "Filters",
    "HIDDEN_HOLD_REASONS",
    "IMAGE_ORIGINS",
    "INITIAL_SYNC_WINDOW_S",
    "SOURCES",
    "Scope",
    "TYPE_KINDS",
    "TYPE_NAMES",
    "VISIBLE_SQL",
    "cutoff_for",
    "date_columns",
    "file_conditions",
    "folder_conditions",
    "glob_match",
    "hidden_sources",
    "make_window",
    "matched_date",
    "parse_filters",
    "resolve_folder_names",
    "resolve_scope",
    "soft_boost",
    "type_of_kind",
]
