"""The query engine: hybrid search, the catalog and the reader over one
profile's index.

Everything here is synchronous — it reads SQLite files and may embed a query —
and is meant to run in ``asyncio.to_thread``; the agent tool and the REST
routes never call it on the event loop.

Access goes through :func:`open_engine`, which answers the question every
caller has first: *can this profile search right now, and if not, why?* The
reasons are the design's state table (feature off, suspended by the admin, no
index yet, still indexing, Vector Embedding off), each phrased so the agent
can pass it on to the user instead of guessing.

Search is hybrid and degrades rather than failing. Every result states its
``mode``:

- ``hybrid`` — vectors and keywords, fused;
- ``hybrid_partial`` — the same, but not every chunk has a vector yet (first
  sync or a re-embed after a model change);
- ``lexical_only`` — no vectors (embedding off, not ready, store down);
- ``vector_only`` — no full-text index (SQLite without FTS5);
- ``catalog_only`` — neither could run: files matching the filters, newest
  first.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.userdocs import types as t
from app.userdocs.query import filters as F
from app.userdocs.query.fusion import (
    RankedList,
    W_LEXICAL,
    W_LEXICAL_IDS,
    W_PHRASE,
    W_VECTOR,
    Group,
    Hit,
    dedupe_by_text,
    drop_noise,
    folder_resolver,
    group_hits,
    rrf,
)
from app.userdocs.query.lexical import search_lexical, search_like
from app.userdocs.query.terms import QueryTerms, analyze
from app.userdocs.query.vector import VectorResult, search_vector
from app.userdocs.textnorm import fold
from app.utils.logger import logger

# Weight of the lists built from a query variant (diacritics restored,
# translated) in thorough mode: useful recall, weaker evidence than the
# user's own words.
W_VARIANT = 0.8

# How long a section may be (estimated tokens) and still be shown whole as a
# passage's context; longer sections show the neighbouring chunks instead.
SECTION_EXPAND_MAX_TOKENS = 1200
# Only the best few results get context: it is the first thing dropped when
# the answer must shrink, and deep results rarely need it.
EXPAND_TOP_GROUPS = 3

# Date relaxation, in order: the window as given, ±3 days, ±14 days, no date.
RELAX_STEPS = ((3, False), (14, False), (0, True))

MODES = ("hybrid", "hybrid_partial", "lexical_only", "vector_only", "catalog_only")

# Soft image constraints: synonyms so "puppy" finds a caption that says "dog".
_OBJECT_SYNONYMS = {
    "puppy": "dog", "puppies": "dog", "dogs": "dog", "chó": "dog", "cho": "dog",
    "kitten": "cat", "kittens": "cat", "cats": "cat", "mèo": "cat", "meo": "cat",
    "people": "person", "persons": "person", "man": "person", "woman": "person", "người": "person",
    "cars": "car", "xe": "car",
}


@dataclass
class SearchOutcome:
    query: str
    mode: str
    mode_reason: str | None
    coverage: float | None
    overview: dict[str, int]
    filters: list[str]
    relaxed: list[str]
    notes: list[str]
    group_by: str
    groups: list[Group]            # this page's results, best first
    total: int                     # results across all pages
    page: int
    top_k: int
    tz_name: str
    tz: _dt.tzinfo | None = None
    # A date window applied: each result's date is the one that matched it.
    date_filtered: bool = False


@dataclass
class Access:
    """What :func:`open_engine` found: an engine, or why there is none."""

    engine: "QueryEngine | None"
    code: str | None = None        # disabled | admin_gate | no_engine | no_index | unknown_profile
    message: str | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)


def _tz_label(tz: _dt.tzinfo) -> str:
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return _dt.datetime.now(tz).tzname() or "local"


def _percent(done: int, total: int) -> int:
    if total <= 0:
        return 100
    return int(100 * done / total) if done < total else 100


def status_notes(snapshot: dict[str, Any]) -> list[str]:
    """Sentences about the index's state that qualify every answer: a
    suspended sync, a held folder, a first sync still running."""
    notes: list[str] = []
    state, reason = snapshot.get("state"), snapshot.get("reason")
    if state == "suspended" and reason == "embedding_off":
        notes.append("Vector Embedding is off: keyword search only, over the index as it was when "
                     "syncing stopped — recent changes may be missing.")
    elif state == "hold":
        notes.append(f"Syncing is on hold ({reason}): results may be outdated.")
    elif state == "paused":
        notes.append(f"Syncing is paused ({reason}): recent changes may be missing.")
    elif state == "awaiting_confirmation" and reason == "mass_delete":
        notes.append("Many files vanished at once; they are hidden until the user confirms or rejects "
                     "removing them (Settings → My Documents).")
    stages = snapshot.get("stages") or {}
    total = sum(int(v or 0) for k, v in stages.items() if k not in ("tombstone", "missing"))
    pending = int(stages.get("dirty") or 0) + int(stages.get("deferred") or 0)
    if total and pending and state in ("indexing", "scanning", "estimating", "reembedding", "idle"):
        notes.append(f"Still indexing: {_percent(total - pending, total)}% of {total:,} files done — "
                     "results may be incomplete.")
    reembed = snapshot.get("reembed") or {}
    if state == "reembedding" and reembed.get("total"):
        done = _percent(int(reembed.get("done") or 0), int(reembed["total"]))
        notes.append(f"Re-indexing for a new embedding model: {done}%.")
    return notes


def _unavailable_message(snapshot: dict[str, Any]) -> tuple[str, str]:
    state, reason = snapshot.get("state"), snapshot.get("reason")
    if state == "suspended" and reason == "admin_gate":
        return "admin_gate", ("User Document Search is disabled by the administrator, so the user's files "
                              "cannot be searched. An admin can allow it in Settings → Embedding.")
    if not snapshot.get("enabled"):
        return "disabled", ("User Document Search is off for this profile. The user can turn it on in "
                            "Settings → My Documents (or `cremind userdocs enable`).")
    if state == "estimating":
        return "no_index", ("User Document Search is still measuring the folder before its first sync; "
                            "try again shortly.")
    if state == "awaiting_confirmation" and reason == "first_sync":
        return "no_index", ("The first sync is waiting for the user's go-ahead (Settings → My Documents, or "
                            "`cremind userdocs start`). Nothing is indexed yet.")
    if state == "hold":
        return "no_index", f"User Document Search is on hold ({reason}) and has not indexed anything yet."
    return "no_index", "The user's files have not been indexed yet; the first sync is starting. Try again shortly."


def _supported(run: dict[str, Any]) -> bool:
    """Whether a pass found real evidence: a keyword or phrase match — or,
    where keyword search could not run at all, any result."""
    if not run["groups"]:
        return False
    if run["mode"] in ("vector_only", "catalog_only"):
        return True
    return any(set(g.evidence) - {"vector"} - {k for k in g.evidence if k.endswith("_vector")}
               for g in run["groups"])


def open_engine(profile: str) -> Access:
    """An engine over ``profile``'s index, or the reason there is none.
    Synchronous (reads the main database and opens the index file)."""
    from app.userdocs import state as uds_state
    from app.userdocs.index import index_path
    from app.userdocs.service import get_service

    try:
        snapshot = uds_state.build_snapshot(profile)
    except Exception as exc:  # noqa: BLE001 — a broken settings read must not crash a tool
        logger.warning(f"[userdocs] status read failed for {profile}: {exc}")
        return Access(None, "no_engine", "User Document Search status could not be read; try again shortly.")
    if not snapshot.get("allowed") or not snapshot.get("enabled") or snapshot.get("tool_mode") == "hidden":
        code, msg = _unavailable_message(snapshot)
        return Access(None, code, msg, snapshot)
    svc = get_service()
    if svc is None:
        return Access(None, "no_engine", "User Document Search is not running on this server.", snapshot)
    rt = svc.runtime(profile, create=True)
    if rt is None:
        return Access(None, "unknown_profile", "Unknown profile.", snapshot)
    if rt.db is None and not os.path.exists(index_path(rt.uid)):
        code, msg = _unavailable_message(snapshot)
        return Access(None, code, msg, snapshot)
    try:
        db = rt.ensure_db()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[userdocs] {profile}: index could not be opened for search: {exc}")
        return Access(None, "no_index", "The document index could not be opened; it is being repaired.", snapshot)

    identity: dict[str, Any] = {}
    try:
        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs import settings as uds

        row = get_userdocs_storage().get_source(profile, uds.SOURCE_LOCAL) or {}
        identity = uds.normalize_options(row.get("options")).get("identity") or {}
    except Exception:  # noqa: BLE001 — identity only boosts; search works without it
        pass
    tz: _dt.tzinfo = _dt.timezone.utc
    try:
        from app.config.timezone import resolve_tzinfo

        tz = resolve_tzinfo(profile)
    except Exception:  # noqa: BLE001
        pass

    def on_stale(file_id: int) -> None:
        # The reader saw a file that changed since it was indexed: queue it
        # ahead of everything else (the "ensure fresh" priority).
        from app.userdocs.runtime import P_INTERACTIVE

        try:
            db.mark_dirty([int(file_id)], priority=P_INTERACTIVE)
            svc.wake()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[userdocs] {profile}: could not queue {file_id} for re-indexing: {exc}")

    engine = QueryEngine(profile, db, tz=tz, identity=identity, snapshot=snapshot, root=rt.root,
                         on_stale=on_stale, runtime=rt)
    return Access(engine, snapshot=snapshot)


class QueryEngine:
    """Search, catalog and reader over one profile's :class:`IndexDB`.

    Cheap to construct (per call); caches rows only for its own lifetime.
    """

    def __init__(
        self,
        profile: str,
        db: Any,
        *,
        tz: _dt.tzinfo | None = None,
        identity: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        root: str | None = None,
        on_stale: Callable[[int], None] | None = None,
        vector_handles: Callable[[], tuple[Any, Any] | None] | None = None,
        runtime: Any = None,
    ) -> None:
        self.profile = profile
        self.db = db
        self.tz = tz or _dt.timezone.utc
        self.tz_name = _tz_label(self.tz)
        self.identity = identity or {}
        self.snapshot = snapshot or {}
        self.root = root
        self.on_stale = on_stale
        # The profile's sync runtime (None in tests and read-only uses):
        # research asks it to re-index what changed before reading.
        self.runtime = runtime
        self._vector_handles = vector_handles
        self._folders: dict[int, dict[str, Any]] | None = None
        self._files: dict[int, dict[str, Any] | None] = {}
        self._cutoff: float | None | bool = False

    # ── row caches ─────────────────────────────────────────────────────────

    def folders(self) -> dict[int, dict[str, Any]]:
        if self._folders is None:
            self._folders = {int(r["id"]): r for r in self.db.folders_brief()}
        return self._folders

    def file(self, file_id: int) -> dict[str, Any] | None:
        fid = int(file_id)
        if fid not in self._files:
            self._files[fid] = self.db.get_file(fid)
        return self._files[fid]

    def files(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        missing = [int(i) for i in ids if int(i) not in self._files]
        if missing:
            found = self.db.files_by_ids(missing)
            for i in missing:
                self._files[i] = found.get(i)
        return {int(i): self._files[int(i)] for i in ids if self._files.get(int(i)) is not None}  # type: ignore[misc]

    def first_seen_cutoff(self) -> float | None:
        """Files first seen after this were found by a live watcher or a
        later scan, so their first_seen_at is a real "appeared" date."""
        if self._cutoff is False:
            try:
                base = self.db.min_first_seen("local")
            except Exception:  # noqa: BLE001
                base = None
            self._cutoff = None if base is None else base + F.INITIAL_SYNC_WINDOW_S
        return self._cutoff  # type: ignore[return-value]

    def vector_handles(self) -> tuple[Any, Any] | None:
        if self.snapshot.get("tool_mode") == "lexical_only":
            return None  # Vector Embedding is off: there is nothing to ask
        if self._vector_handles is not None:
            return self._vector_handles()
        from app.userdocs.vector_sync import live_handles

        return live_handles()

    def overview(self) -> dict[str, int]:
        try:
            return self.db.query_overview()
        except Exception:  # noqa: BLE001
            return {"files": 0, "pending": 0, "awaiting_captions": 0, "unreadable": 0}

    def coverage(self, gen: int | None) -> float | None:
        if gen is None:
            return None
        try:
            done, total = self.db.vector_coverage(gen)
        except Exception:  # noqa: BLE001
            return None
        return (done / total) if total else 1.0

    def scope(self, f: F.Filters, *, widen_days: int = 0, ignore_date: bool = False) -> F.Scope:
        return F.resolve_scope(
            self.db, f,
            folders=list(self.folders().values()) if f.folders else None,
            tz=self.tz, widen_days=widen_days,
            first_seen_cutoff=self.first_seen_cutoff() if f.date_field == "any" else None,
            ignore_date=ignore_date,
        )

    def acceptor(self, scope: F.Scope) -> Callable[[dict[str, Any]], bool]:
        """Is this chunk row a valid hit for ``scope``? Its file must be
        visible and in scope; a folder card's folder must be allowed."""
        def accept(row: dict[str, Any]) -> bool:
            fid = row.get("file_id")
            if fid is not None:
                return scope.accepts_file(self.file(int(fid)))
            folder_id = row.get("folder_id")
            if folder_id is None:
                return False
            return scope.accepts_folder(self.folders().get(int(folder_id)))
        return accept

    def _filter_ids(self, ids: list[int], accept: Callable[[dict[str, Any]], bool],
                    rows: dict[int, dict[str, Any]]) -> list[int]:
        return [cid for cid in dict.fromkeys(ids) if cid in rows and accept(rows[cid])]

    # ── search ─────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        filters: Any = None,
        group_by: str = "file",
        top_k: int = 8,
        expand: bool = True,
        variants: list[str] | None = None,
        image_objects: list[dict[str, Any]] | None = None,
        verify_images: bool = False,
        page: int = 1,
    ) -> SearchOutcome:
        f = filters if isinstance(filters, F.Filters) else F.parse_filters(filters)
        if group_by not in ("file", "folder", "chunk"):
            raise F.FilterError("group_by must be file, folder or chunk")
        top_k = max(1, min(int(top_k or 8), 50))
        page = max(1, int(page or 1))
        terms = analyze(query)
        notes = status_notes(self.snapshot)
        limit = min(400, max(60, top_k * page * 8))

        relaxed: list[str] = []
        scope = self.scope(f)
        run = self._search_once(terms, scope, f, group_by, limit, variants or [], image_objects)
        if f.has_date and not scope.unresolved and not _supported(run):
            run, scope, relaxed = self._relax(
                run, scope, lambda s: self._search_once(terms, s, f, group_by, limit, variants or [],
                                                        image_objects),
                f,
            )
        notes += scope.notes

        groups: list[Group] = run["groups"]
        if verify_images:
            notes += self._verify_images(groups, image_objects or [], terms.raw)
            groups.sort(key=lambda g: -g.score)
        total = len(groups)
        start = (page - 1) * top_k
        shown = groups[start:start + top_k]
        if expand:
            self.expand(shown)
        return SearchOutcome(
            query=terms.raw, mode=run["mode"], mode_reason=run["reason"], coverage=run["coverage"],
            overview=self.overview(), filters=f.describe(self.tz_name), relaxed=relaxed, notes=notes,
            group_by=group_by, groups=shown, total=total, page=page, top_k=top_k, tz_name=self.tz_name,
            tz=self.tz, date_filtered=scope.window is not None,
        )

    def _relax(self, run: dict[str, Any], scope: F.Scope, again: Callable[[F.Scope], dict[str, Any]],
               f: F.Filters) -> tuple[dict[str, Any], F.Scope, list[str]]:
        """Date relaxation: ±3 days, ±14 days, then no date, until a step
        finds *keyword evidence*.

        Vector search always returns the nearest passages, relevant or not,
        so "something came back" cannot mean "the window matched". A window
        whose results have no keyword support counts as a miss; if no wider
        step finds support either, the original window's results (or the
        first non-empty wider step's) are kept and the result says so."""
        first_label = scope.window.label if scope.window else ""
        fallback: tuple[dict[str, Any], F.Scope, str] | None = (run, scope, "") if run["groups"] else None
        for widen, drop in RELAX_STEPS:
            wider = self.scope(f, widen_days=widen, ignore_date=drop)
            attempt = again(wider)
            step = "dropped the date filter" if drop else f"widened to ±{widen} days ({wider.window.label})"
            if _supported(attempt):
                return attempt, wider, [f"nothing matched {first_label}; {step}"]
            if attempt["groups"] and fallback is None:
                fallback = (attempt, wider, step)
        if fallback is None:
            return run, scope, [f"nothing matched {first_label}, even without the date filter"]
        kept, kept_scope, step = fallback
        if not step:
            return kept, kept_scope, [f"no keyword match in {first_label}, even with wider dates; "
                                      "showing the closest passages in that window"]
        return kept, kept_scope, [f"nothing matched {first_label}; {step} (closest passages only)"]

    def _search_once(
        self,
        terms: QueryTerms,
        scope: F.Scope,
        f: F.Filters,
        group_by: str,
        limit: int,
        variants: list[str],
        image_objects: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """One pass at a fixed scope: run the lists, fuse, boost, dedupe and
        group. Returns ``{groups, mode, reason, coverage}``."""
        if scope.empty:
            return {"groups": [], "mode": "catalog_only", "reason": "a filter matched nothing", "coverage": None}
        accept = self.acceptor(scope)

        lex = search_lexical(self.db, terms, scope=scope, limit=limit)
        handles = self.vector_handles()
        vec: VectorResult = search_vector(self.db, terms.raw, scope=scope, k=limit, accept=accept,
                                          handles=handles) if handles is not None else \
            VectorResult(available=False, reason="Vector Embedding is off" if
                         self.snapshot.get("tool_mode") == "lexical_only" else "Vector Embedding is not ready")
        if not lex.available and not vec.available and self.db.lexical != "fts5":
            lex = search_like(self.db, terms, scope=scope, limit=limit)

        lists: list[RankedList] = []
        lex_ids = list(lex.main) + list(lex.phrase)
        variant_lists: list[RankedList] = []
        if variants:
            variant_lists = self._variant_lists(variants, scope, limit, accept, handles, vec.available)
        rows = {int(r["id"]): r for r in self.db.chunk_rows(
            lex_ids + [cid for cid, _ in vec.hits] + [i for vl in variant_lists for i in vl.ids])}
        if lex.available:
            w = W_LEXICAL_IDS if terms.has_ids_or_numbers else W_LEXICAL
            lists.append(RankedList("lexical", w, self._filter_ids(lex.main, accept, rows)))
            if lex.phrase:
                lists.append(RankedList("phrase", W_PHRASE, self._filter_ids(lex.phrase, accept, rows)))
        if vec.available:
            lists.append(RankedList("vector", W_VECTOR, [cid for cid, _ in vec.hits]))
        lists += variant_lists

        if lex.available and not lex.like and vec.available:
            coverage = self.coverage(vec.gen)
            mode = "hybrid" if coverage is None or coverage >= 0.999 else "hybrid_partial"
            reason = None if mode == "hybrid" else \
                f"vectors cover {int((coverage or 0) * 100)}% of the index so far; the rest is found by keyword"
        elif lex.available and lex.like:
            mode, coverage = "lexical_only", None
            reason = f"{vec.reason}; no full-text index either, so a slow keyword scan was used"
        elif lex.available:
            mode, coverage, reason = "lexical_only", None, vec.reason
        elif vec.available:
            mode, coverage, reason = "vector_only", self.coverage(vec.gen), lex.reason
        else:
            return {"groups": self._catalog_groups(scope, f, limit), "mode": "catalog_only",
                    "reason": f"{vec.reason}; {lex.reason}", "coverage": None}

        fused = rrf(lists)
        hits: list[Hit] = []
        for cid, (score, ranks) in fused.items():
            row = rows.get(cid)
            if row is None:
                continue
            file_row = self.file(int(row["file_id"])) if row.get("file_id") is not None else None
            folder = self.folders().get(int(row["folder_id"])) if row.get("folder_id") is not None else None
            hit = Hit(chunk=row, file=file_row, folder=folder, score=score, ranks=ranks)
            if file_row is not None:
                factor, reasons = F.soft_boost(file_row, f, self.identity)
                if image_objects and file_row.get("kind") == t.KIND_IMAGE:
                    factor2, why = self._image_object_factor(file_row, image_objects)
                    factor *= factor2
                    reasons += why
                hit.score *= factor
                hit.reasons = reasons
                hit.date = F.matched_date(file_row, f.date_field, scope.window, scope.first_seen_cutoff)
            hits.append(hit)
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        hits = dedupe_by_text(hits)
        groups = group_hits(hits, group_by, folder_for_file=folder_resolver(self.folders())
                            if group_by == "folder" else (lambda _r: None))
        return {"groups": drop_noise(groups), "mode": mode, "reason": reason, "coverage": coverage}

    def _variant_lists(self, variants: list[str], scope: F.Scope, limit: int,
                       accept: Callable[[dict[str, Any]], bool], handles: Any,
                       vector_ok: bool) -> list[RankedList]:
        """Extra ranked lists for thorough mode's query variants (diacritics
        restored, translated), at a lower weight than the user's own query."""
        out: list[RankedList] = []
        for i, v in enumerate(variants[:3]):
            vt = analyze(v)
            if vt.empty:
                continue
            lex = search_lexical(self.db, vt, scope=scope, limit=limit)
            if lex.available and lex.main:
                rows = {int(r["id"]): r for r in self.db.chunk_rows(lex.main)}
                out.append(RankedList(f"variant{i}_lexical", W_VARIANT, self._filter_ids(lex.main, accept, rows)))
            if vector_ok and handles is not None:
                vr = search_vector(self.db, v, scope=scope, k=limit, accept=accept, handles=handles)
                if vr.available and vr.hits:
                    out.append(RankedList(f"variant{i}_vector", W_VARIANT, [cid for cid, _ in vr.hits]))
        return out

    def _catalog_groups(self, scope: F.Scope, f: F.Filters, limit: int) -> list[Group]:
        """``catalog_only``: files in scope, newest first, each shown by its
        card — what is left when neither keyword nor vector search can run."""
        if scope.file_ids is not None:
            rows = sorted(self.files(scope.file_ids[:5000]).values(), key=lambda r: -(r.get("mtime") or 0))
        else:
            rows = self.db.read_sql(
                f"SELECT * FROM files WHERE {F.VISIBLE_SQL} ORDER BY mtime DESC LIMIT ?", (limit,), table="files")
        rows = [r for r in rows if scope.accepts_file(r)][:limit]
        cards = {int(r["file_id"]): r for r in self.db.read_sql(
            "SELECT * FROM chunks WHERE ctype = 'file_card' AND file_id IN (" + ",".join("?" * len(rows)) + ")",
            [int(r["id"]) for r in rows], table="chunks")} if rows else {}
        out: list[Group] = []
        for n, r in enumerate(rows):
            card = cards.get(int(r["id"]))
            if card is None:
                continue
            hit = Hit(chunk=card, file=r, folder=None, score=1.0 / (60 + n + 1),
                      date=F.matched_date(r, f.date_field, scope.window, scope.first_seen_cutoff))
            out.append(Group(kind="file", key=("f", int(r["id"])), file=r, folder=None, score=hit.score,
                             passages=[hit]))
        return out

    VERIFY_MAX = 6

    def _verify_images(self, groups: list[Group], objects: list[dict[str, Any]], query: str) -> list[str]:
        """Look again at the top image results with the Specialized Vision
        Model (never the main model), and re-rank by what it actually sees.

        A caption can miscount ("two puppies" vs three); when the user asked
        for something specific this checks the few images that matter. It
        needs the same consent and daily quota as captioning, and says so —
        in the result — when it cannot run."""
        from app.storage.userdocs_storage import get_userdocs_storage
        from app.userdocs import settings as uds
        from app.userdocs.discovery.hashing import fs_path
        from app.userdocs.vision import captioner, resolver

        targets = [g for g in groups[: self.VERIFY_MAX * 3]
                   if g.file and g.file.get("kind") == t.KIND_IMAGE][: self.VERIFY_MAX]
        if not targets:
            return []
        if not self.root:
            return ["Image check skipped: the indexed folder is not available."]
        res = resolver.resolve_dedicated_vision(self.profile)
        if not res.ok:
            return [f"Image check unavailable ({res.reason}); ranking uses captions only."]
        storage = get_userdocs_storage()
        opts = uds.normalize_options((storage.get_source(self.profile, uds.SOURCE_LOCAL) or {}).get("options"))
        if not resolver.consent_matches(opts, res):
            return ["Image check unavailable: sending images to the vision model has not been allowed."]
        cap = captioner.daily_cap(opts)
        day = captioner.local_day(self.profile)
        wanted: dict[str, Any] = {}
        for o in objects[:5]:
            label = fold(str(o.get("label") or ""))
            if label:
                wanted[_OBJECT_SYNONYMS.get(label, label)] = o.get("count")
        labels = list(wanted)
        root = uds.real_path(self.root)
        llm = None
        checked = 0
        for g in targets:
            # Only a file that really sits under the indexed folder is sent: a
            # symlink swapped in since indexing must not leak another image.
            path = uds.real_path(os.path.join(root, *str(g.file["rel_path"]).split("/")))
            if not uds.is_inside(path, root) or not os.path.isfile(fs_path(path)):
                continue
            if not storage.reserve_vision(self.profile, day, cap):
                return [f"Image check stopped after {checked}: today's image quota is used up."]
            try:
                jpeg = captioner.prepare_jpeg(path=fs_path(path))
                llm = llm or resolver.build_vision_llm(self.profile, res)
                verdict = captioner.run_verify(llm, self.profile, jpeg, labels=labels, query=query)
            except Exception as exc:  # noqa: BLE001 — a failed check leaves the ranking as it was
                storage.refund_vision(self.profile, day)
                logger.info(f"[userdocs] {self.profile}: image check failed for fid "
                            f"{g.file.get('cite_id')}: {exc}")
                continue
            checked += 1
            factor = 1.0
            why: list[str] = []
            seen_by = {_OBJECT_SYNONYMS.get(fold(k), fold(k)): v for k, v in verdict["counts"].items()}
            for label, count in wanted.items():
                seen = seen_by.get(label, seen_by.get(label + "s"))
                if seen is None or count is None:
                    continue
                if int(seen) == int(count):
                    factor *= 1.6
                    why.append(f"checked: {seen} {label}")
                else:
                    factor *= 0.5
                    why.append(f"checked: {seen} {label}, not {count}")
            if verdict["matches"] is False:
                factor *= 0.6
                why.append("checked: does not match the description")
            elif verdict["matches"] is True:
                factor *= 1.2
            g.score *= factor
            if why and g.passages:
                g.passages[0].reasons.extend(why)
        return [f"{checked} image(s) re-checked with the vision model."] if checked else []

    def _image_object_factor(self, file_row: dict[str, Any], objects: list[dict[str, Any]]) -> tuple[float, list[str]]:
        """Soft check of ``image_objects`` against the image's caption: +50%
        for a label with the right count, −30% for a stated wrong count, no
        change when the image has no caption yet."""
        caption_rows = self.db.read_sql(
            "SELECT text FROM chunks WHERE file_id = ? AND ctype = ?", (int(file_row["id"]), t.CTYPE_CAPTION))
        if not caption_rows:
            return 1.0, []
        text = fold(" ".join(r["text"] or "" for r in caption_rows))
        factor = 1.0
        reasons: list[str] = []
        for obj in objects[:5]:
            label = fold(str(obj.get("label") or ""))
            if not label:
                continue
            canon = _OBJECT_SYNONYMS.get(label, label)
            forms = {label, canon, canon + "s"}
            if not any(fm in text for fm in forms):
                continue
            count = obj.get("count")
            if count is None:
                factor *= 1.2
                reasons.append(f"caption mentions {canon}")
                continue
            said = [int(m.group(1)) for fm in forms for m in re.finditer(rf"(\d+)\s+{re.escape(fm)}", text)]
            if int(count) in said:
                factor *= 1.5
                reasons.append(f"caption: {count} {canon}")
            elif said:
                factor *= 0.7
        return factor, reasons

    def expand(self, groups: list[Group]) -> None:
        """Attach context to the best passage of the first few ``groups``."""
        for g in groups[:EXPAND_TOP_GROUPS]:
            self._expand(g.best)

    def _expand(self, hit: Hit) -> None:
        """Attach context to a passage: its whole section when that is short
        (a legal article, a small heading section), else its neighbours."""
        row = hit.chunk
        fid = row.get("file_id")
        if fid is None or row.get("ctype") not in (t.CTYPE_BODY, t.CTYPE_OCR):
            return
        ordinal = int(row.get("ordinal") or 0)
        key = row.get("section_key")
        try:
            if key:
                section = self.db.read_sql(
                    "SELECT * FROM chunks WHERE file_id = ? AND section_key = ? ORDER BY ordinal, id",
                    (int(fid), key), table="chunks")
                if len(section) <= 1:
                    return  # the passage is its whole section (a short article): complete as is
                size = sum(int(r.get("token_est") or 0) for r in section)
                if size <= SECTION_EXPAND_MAX_TOKENS:
                    hit.expansion = [r for r in section if int(r["id"]) != hit.chunk_id]
                    return
            near = self.db.read_sql(
                "SELECT * FROM chunks WHERE file_id = ? AND ordinal IN (?, ?) AND ordinal >= 0 "
                "AND ctype IN (?, ?) ORDER BY ordinal, id",
                (int(fid), ordinal - 1, ordinal + 1, t.CTYPE_BODY, t.CTYPE_OCR), table="chunks")
            hit.expansion = [r for r in near if int(r["id"]) != hit.chunk_id]
        except Exception as exc:  # noqa: BLE001 — context is optional
            logger.debug(f"[userdocs] expansion failed for chunk {hit.chunk_id}: {exc}")

    # ── catalog and reader ─────────────────────────────────────────────────

    def find(self, query: str | None = None, **kw: Any):
        from app.userdocs.query.catalog import find

        return find(self, query, **kw)

    def read(self, file: str, **kw: Any):
        from app.userdocs.query.reader import read

        return read(self, file, **kw)


__all__ = ["Access", "MODES", "QueryEngine", "SearchOutcome", "open_engine", "status_notes"]
