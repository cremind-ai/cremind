"""Rendering query results for the agent (and the CLI), within a budget.

The reasoning agent head-clips every tool result to the profile's
``tool_result.max_tokens``. A clipped search result would lose exactly what
matters — the citation tokens of the passages it cut — so every renderer here
sizes its own output and shrinks it deliberately instead:

- ``search`` drops context expansions first, then shortens snippets, then
  shows fewer results (and says how to page to the rest). The header and every
  token that is printed survive; a token is never cut in half.
- ``read`` shows the whole selection when it fits; otherwise, for the whole
  file, an envelope (beginning, table of contents with part sizes, parts
  matching the query), and for an explicit selection, the selection in parts.
- ``find_files`` lists fewer entries when it must, with a page cursor.

Everything derived from the user's files — names, paths, snippets — is
untrusted content: it goes inside one document-content block per result, and
any ``[doc:`` inside it is defanged, so a file cannot forge a citation or a
block boundary. The renderers also return the tokens they printed, as
:class:`~app.documents.cite.IssuedCitation` records for the citation registry,
and the image thumbnails to attach as file chips.

Token counting, line fitting and the content wrapper are injected (see
:class:`RenderContext`), so this module does not depend on the tool layer.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.documents import types as t
from app.documents.cite import IssuedCitation, escape_in_document_text, locator_label, make_token
from app.documents.query.filters import DATE_LABELS, type_of_kind
from app.documents.query.terms import analyze
from app.documents.textnorm import fold

LEAF_FIND = "find_files"
LEAF_SEARCH = "search"
LEAF_READ = "read"

# Exposed function names, as the agent sees them (``make_leaf_name`` of the
# ``documentation_search`` group); the tool module pins them against the registry.
FN_FIND = "documentation_search__find_files"
FN_SEARCH = "documentation_search__search"
FN_READ = "documentation_search__read"

MAX_IMAGE_CHIPS = 12
SNIPPET_MAX_CHARS = 800            # the registry's cap on a stored snippet
_SNIPPET_STEPS = (600, 320, 180)
_WS_RE = re.compile(r"\s+")


@dataclass
class RenderContext:
    """What a renderer needs from its host: the budget in tokens (None: no
    limit, e.g. the CLI), the token counter the budget is measured in, the
    line fitter and cutter that respect it, and the untrusted-content
    wrapper."""

    limit: int | None
    tokens: Callable[[str], int]
    fit_lines: Callable[..., tuple[str, bool]]
    cut_tokens: Callable[[str, int], str]
    wrap: Callable[[str], str]
    thumbnail_uri: Callable[[str], str] = lambda fid: f"/api/documentation-search/files/{fid}/thumbnail"

    def fits(self, text: str) -> bool:
        return self.limit is None or self.tokens(text) <= self.limit


@dataclass
class Rendered:
    text: str
    citations: list[IssuedCitation] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


# ── small formatting helpers ───────────────────────────────────────────────


def _day(ts: Any, tz: _dt.tzinfo) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts), tz).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _size(n: Any) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{int(v)} B" if unit == "B" else (f"{v:.1f} {unit}" if v < 10 else f"{v:.0f} {unit}")
        v /= 1024
    return f"{int(n or 0)} B"


def _clean(text: str) -> str:
    """Document text made safe to print: a ``[doc:`` inside it can no longer
    be read as a citation."""
    return escape_in_document_text(text or "")


def _one_line(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _snippet(text: str, query_tokens: tuple[str, ...], max_chars: int) -> str:
    """At most ``max_chars`` of ``text`` on one line, centred on the first
    query word it contains, cut at word boundaries, with … where cut."""
    flat = _one_line(text)
    if len(flat) <= max_chars:
        return flat
    hay = fold(flat)
    pos = min((hay.find(fold(tok)) for tok in query_tokens if fold(tok) and fold(tok) in hay), default=0)
    start = max(0, pos - max_chars // 4)
    if start:
        space = flat.find(" ", start)
        start = space + 1 if 0 <= space < start + 40 else start
    end = min(len(flat), start + max_chars)
    if end < len(flat):
        space = flat.rfind(" ", start, end)
        end = space if space > start + max_chars // 2 else end
    return ("… " if start else "") + flat[start:end].strip() + (" …" if end < len(flat) else "")


def _n(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def overview_line(overview: dict[str, int]) -> str:
    """"12,480 files · 97% synced · 312 images awaiting captions · 3 unreadable"."""
    files = int(overview.get("files") or 0)
    pending = int(overview.get("pending") or 0)
    synced = 100 if not files else int(100 * max(0, files - pending) / files)
    parts = [_n(files, "file", "files"), f"{synced}% synced"]
    if overview.get("awaiting_captions"):
        parts.append(_n(int(overview["awaiting_captions"]), "image awaiting a caption", "images awaiting captions"))
    if overview.get("unreadable"):
        parts.append(f"{int(overview['unreadable']):,} unreadable")
    return " · ".join(parts)


def _mode_lines(mode: str, reason: str | None) -> list[str]:
    if mode in ("hybrid", "catalog"):
        return []
    what = {
        "hybrid_partial": "keyword + partial vector search",
        "lexical_only": "keyword search only",
        "vector_only": "vector search only",
        "catalog_only": "file names and details only",
    }.get(mode, mode)
    return [f"Mode {mode}: {what}" + (f" — {reason}." if reason else ".")]


def _label_for(chunk: dict[str, Any]) -> str:
    if chunk.get("ctype") == t.CTYPE_FILE_CARD:
        return "file details"
    if chunk.get("ctype") == t.CTYPE_FOLDER_CARD:
        return "folder details"
    if chunk.get("ctype") == t.CTYPE_CAPTION:
        return "image caption"
    label = locator_label(chunk.get("locator") or {})
    if chunk.get("ctype") == t.CTYPE_OCR:
        label = (label + ", " if label else "") + "scanned text"
    return label


# ── citations ──────────────────────────────────────────────────────────────


def cite_file(row: dict[str, Any], leaf: str) -> IssuedCitation:
    return IssuedCitation(
        token=make_token(row["cite_id"]), cite_id=row["cite_id"], target="file", ref_id=int(row["id"]),
        source_kind=row.get("source") or "local", rel_path=row.get("rel_path") or "",
        leaf=leaf, web_link=row.get("drive_web_link"),
    )


def cite_folder(row: dict[str, Any], leaf: str) -> IssuedCitation:
    return IssuedCitation(
        token=make_token(row["cite_id"]), cite_id=row["cite_id"], target="folder", ref_id=int(row["id"]),
        source_kind=row.get("source") or "local", rel_path=row.get("rel_path") or "", leaf=leaf,
    )


def cite_chunk(owner: dict[str, Any], chunk: dict[str, Any], leaf: str, *, folder: bool = False) -> IssuedCitation:
    loc = chunk.get("locator") or {}
    return IssuedCitation(
        token=make_token(owner["cite_id"], chunk.get("text_hash") or ""), cite_id=owner["cite_id"],
        target="folder" if folder else "file", ref_id=int(owner["id"]),
        text_hash=chunk.get("text_hash"), source_kind=owner.get("source") or "local",
        locator=dict(loc) if isinstance(loc, dict) else {}, label=_label_for(chunk),
        rel_path=owner.get("rel_path") or "", snippet=(chunk.get("text") or "")[:SNIPPET_MAX_CHARS],
        leaf=leaf, web_link=None if folder else owner.get("drive_web_link"),
    )


class _Issued:
    """Collects the citations a render printed, each token once."""

    def __init__(self) -> None:
        self.items: dict[str, IssuedCitation] = {}

    def add(self, c: IssuedCitation) -> str:
        self.items.setdefault(c.token, c)
        return c.token

    @property
    def list(self) -> list[IssuedCitation]:
        return list(self.items.values())


def _chip(row: dict[str, Any], ctx: RenderContext) -> dict[str, Any]:
    return {
        "uri": ctx.thumbnail_uri(row["cite_id"]),
        "name": row.get("name") or row.get("rel_path") or row["cite_id"],
        "mime_type": "image/jpeg",
        "origin": "referenced",
    }


def _file_line(n: int, row: dict[str, Any], issued: _Issued, leaf: str, tz: _dt.tzinfo,
               date: tuple[str, float] | None, extra: list[str], *, date_matched: bool = False) -> str:
    token = issued.add(cite_file(row, leaf))
    bits = [_clean(row.get("rel_path") or ""), type_of_kind(row.get("kind")), _size(row.get("size"))]
    if date:
        # Under a date filter, say which of the file's dates put it in the
        # window — "any" can match a creation, a change or an EXIF date.
        col, ts = date
        bits.append(f"{'date matched: ' if date_matched else ''}{DATE_LABELS.get(col, col)} {_day(ts, tz)}")
    elif row.get("mtime"):
        bits.append(f"modified {_day(row['mtime'], tz)}")
    if row.get("kind") == t.KIND_IMAGE:
        exif = row.get("exif") or {}
        camera = " ".join(str(exif.get(k)) for k in ("make", "model") if exif.get(k))
        if camera:
            bits.append(_clean(camera))
        state = row.get("caption_state")
        if state in ("awaiting_vision", "awaiting_consent", "over_cap", "failed"):
            bits.append("no caption yet (found by name, date and camera)")
        elif state == "captions_off":
            bits.append("no caption (image descriptions are off)")
        elif state == "skipped_small":
            bits.append("no caption (too small: icon or thumbnail)")
    if row.get("status") in ("metadata_only", "awaiting_extractor", "error"):
        bits.append(f"content not indexed ({row.get('status_reason') or row.get('status')})")
    bits += extra
    return f"{n}. {_clean(row.get('name') or '')} {token} — " + " · ".join(b for b in bits if b)


def _folder_line(n: int, row: dict[str, Any] | None, issued: _Issued, leaf: str, tz: _dt.tzinfo,
                 extra: list[str]) -> str:
    if row is None:
        return f"{n}. Top level of the indexed folder — " + " · ".join(extra)
    token = issued.add(cite_folder(row, leaf))
    meta = row.get("project_meta") if isinstance(row.get("project_meta"), dict) else {}
    bits = [_clean(row.get("rel_path") or ""), "project" if row.get("is_project") else "folder"]
    langs = meta.get("languages") if isinstance(meta, dict) else None
    if isinstance(langs, dict) and langs:
        top = sorted(langs.items(), key=lambda kv: -int(kv[1] or 0))[:3]
        bits.append(", ".join(f"{_clean(str(k))} ({v})" for k, v in top))
    if row.get("file_count"):
        bits.append(_n(int(row["file_count"]), "file", "files") + " at its top level")
    bits += extra
    return f"{n}. Folder {_clean(row.get('name') or row.get('rel_path') or '')} {token} — " + " · ".join(
        b for b in bits if b)


# ── search ─────────────────────────────────────────────────────────────────


def _passage_line(hit: Any, issued: _Issued, leaf: str, *, snippet_chars: int, tokens: tuple[str, ...],
                  with_file: bool) -> str | None:
    chunk = hit.chunk
    owner = hit.file if hit.file is not None else hit.folder
    if owner is None:
        return None
    token = issued.add(cite_chunk(owner, chunk, leaf, folder=hit.file is None))
    label = _label_for(chunk)
    # Grouped by folder, a passage names the file it is from.
    prefix = f"{_clean(owner.get('name') or '')} › " if with_file else ""
    text = _snippet(_clean(chunk.get("text") or ""), tokens, snippet_chars)
    return f"   {prefix}{label + ' ' if label else ''}{token}: {text}"


def _search_body(outcome: Any, groups: list[Any], issued: _Issued, *, expand: bool, snippet_chars: int,
                 passages: int, tz: _dt.tzinfo, offset: int) -> str:
    tokens = analyze(outcome.query).tokens
    out: list[str] = []
    for i, g in enumerate(groups, start=offset + 1):
        best = g.best
        conf = f"confidence {best.confidence}"
        if g.kind == "file" and g.file is not None:
            out.append(_file_line(i, g.file, issued, LEAF_SEARCH, tz, best.date, [conf, *best.reasons],
                                  date_matched=getattr(outcome, "date_filtered", False)))
            with_file = False
        else:
            out.append(_folder_line(i, g.folder, issued, LEAF_SEARCH, tz, [conf]))
            with_file = True
        for hit in g.passages[:passages]:
            line = _passage_line(hit, issued, LEAF_SEARCH, snippet_chars=snippet_chars, tokens=tokens,
                                 with_file=with_file and hit.file is not None)
            if line:
                out.append(line)
            if expand and hit.expansion and hit.file is not None:
                for ctx_chunk in hit.expansion:
                    token = issued.add(cite_chunk(hit.file, ctx_chunk, LEAF_SEARCH))
                    label = _label_for(ctx_chunk)
                    text = _snippet(_clean(ctx_chunk.get("text") or ""), tokens, snippet_chars)
                    out.append(f"   context {label + ' ' if label else ''}{token}: {text}")
            if hit.also_in:
                also = ", ".join(
                    f"{_clean(o.get('rel_path') or '')} {issued.add(cite_file(o, LEAF_SEARCH))}"
                    for o in hit.also_in[:3])
                more = f" (+{len(hit.also_in) - 3} more)" if len(hit.also_in) > 3 else ""
                out.append(f"   same text also in: {also}{more}")
    return "\n".join(out)


def render_search(outcome: Any, ctx: RenderContext) -> Rendered:
    """The ``search`` result: header, one block of results, footer — shrunk
    to ``ctx.limit`` (expansions → snippet length → passages → results)."""
    tz = _tz_of(outcome)
    head = [f"[Documentation Search · search · \"{_one_line(_clean(outcome.query))}\" · mode: {outcome.mode} · "
            f"{overview_line(outcome.overview)}]"]
    head += _mode_lines(outcome.mode, outcome.mode_reason)
    if outcome.filters:
        head.append("Filters: " + "; ".join(outcome.filters))
    for r in outcome.relaxed:
        head.append(f"Date relaxed: {r}.")
    for note in outcome.notes:
        head.append(f"Note: {note}")
    offset = (outcome.page - 1) * outcome.top_k
    groups = list(outcome.groups)

    def assemble(n: int, expand: bool, chars: int, passages: int) -> tuple[str, _Issued]:
        issued = _Issued()
        shown = groups[:n]
        if not shown:
            if outcome.total and offset >= outcome.total:
                summary = f"No results on page {outcome.page}; there are {outcome.total} in all."
            else:
                summary = "No results. Try other words, fewer filters, or documentation_search__find_files by name."
            return "\n".join(head + [summary]), issued
        summary = (f"Results {offset + 1}–{offset + len(shown)} of {outcome.total} "
                   f"(grouped by {outcome.group_by}; best first):")
        body = _search_body(outcome, shown, issued, expand=expand, snippet_chars=chars, passages=passages,
                            tz=tz, offset=offset)
        foot = [
            "Cite every claim with the [doc:…] token printed next to the passage it comes from, copied "
            f"exactly. Read more of a file with {FN_READ} (file=its token).",
        ]
        remaining = outcome.total - (offset + len(shown))
        if remaining > 0:
            if len(shown) < outcome.top_k:
                nxt = (offset + len(shown)) // len(shown) + 1
                foot.append(f"{remaining} more: call again with top_k={len(shown)} and page={nxt}.")
            else:
                foot.append(f"{remaining} more: call again with page={outcome.page + 1}.")
        text = "\n".join(head + [summary, ctx.wrap(body)] + foot)
        return text, issued

    levels = [(True, _SNIPPET_STEPS[0], 2), (False, _SNIPPET_STEPS[0], 2), (False, _SNIPPET_STEPS[1], 2),
              (False, _SNIPPET_STEPS[1], 1), (False, _SNIPPET_STEPS[2], 1)]
    n = len(groups)
    text, issued, level = "", _Issued(), levels[0]
    for level in levels:
        text, issued = assemble(n, *level)
        if ctx.fits(text):
            break
    else:
        while n > 1 and not ctx.fits(text):
            n -= 1
            text, issued = assemble(n, *level)
        chars = level[1]
        while not ctx.fits(text) and chars > 40:
            chars = max(40, chars // 2)
            text, issued = assemble(n, False, chars, 1)
    shown = groups[:n]
    files = [_chip(g.file, ctx) for g in shown if g.file is not None and g.file.get("kind") == t.KIND_IMAGE]
    return Rendered(text=text, citations=issued.list, files=files[:MAX_IMAGE_CHIPS],
                    data=_search_data(outcome, shown, issued, tz))


def _search_data(outcome: Any, shown: list[Any], issued: _Issued, tz: _dt.tzinfo) -> dict[str, Any]:
    items = []
    for g in shown:
        owner = g.file if g.file is not None else g.folder
        passages = []
        for h in g.passages:
            o = h.file if h.file is not None else h.folder
            if o is None:
                continue
            passages.append({
                "token": make_token(o["cite_id"], h.chunk.get("text_hash") or ""),
                "label": _label_for(h.chunk),
                "file": o.get("rel_path"),
                "snippet": _one_line(h.chunk.get("text") or "")[:SNIPPET_MAX_CHARS],
                "score": round(h.score, 6),
                "confidence": h.confidence,
            })
        items.append({
            "kind": g.kind,
            "fid": owner.get("cite_id") if owner else None,
            "token": make_token(owner["cite_id"]) if owner else None,
            "name": owner.get("name") if owner else None,
            "rel_path": owner.get("rel_path") if owner else None,
            "score": round(g.score, 6),
            "date": ({"field": g.best.date[0], "day": _day(g.best.date[1], tz)} if g.best.date else None),
            "passages": passages,
        })
    return {
        "leaf": LEAF_SEARCH, "query": outcome.query, "mode": outcome.mode, "mode_reason": outcome.mode_reason,
        "coverage": outcome.coverage, "overview": outcome.overview, "filters": outcome.filters,
        "relaxed": outcome.relaxed, "notes": outcome.notes, "group_by": outcome.group_by,
        "total": outcome.total, "page": outcome.page, "top_k": outcome.top_k, "shown": len(shown),
        "items": items,
    }


def _tz_of(outcome: Any) -> _dt.tzinfo:
    tz = getattr(outcome, "tz", None)
    if tz is not None:
        return tz
    name = getattr(outcome, "tz_name", None)
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — "UTC+07", "local": fall back to local time
            pass
    return _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc


# ── find_files ─────────────────────────────────────────────────────────────


def render_find(outcome: Any, ctx: RenderContext) -> Rendered:
    tz = _tz_of(outcome)
    q = f" · \"{_one_line(_clean(outcome.query))}\"" if outcome.query else ""
    head = [f"[Documentation Search · find_files{q} · kind={outcome.kind} · mode: {outcome.mode} · "
            f"{overview_line(outcome.overview)}]"]
    head += _mode_lines(outcome.mode, outcome.mode_reason)
    if outcome.filters:
        head.append("Filters: " + "; ".join(outcome.filters))
    for r in outcome.relaxed:
        head.append(f"Date relaxed: {r}.")
    for note in outcome.notes:
        head.append(f"Note: {note}")
    offset = (outcome.page - 1) * outcome.limit
    items = list(outcome.items)

    def assemble(n: int) -> tuple[str, _Issued]:
        issued = _Issued()
        lines: list[str] = []
        for i, it in enumerate(items[:n], start=offset + 1):
            if it.kind == "file":
                extra = list(it.reasons)
                lines.append(_file_line(i, it.row, issued, LEAF_FIND, tz, it.date, extra,
                                        date_matched=getattr(outcome, "date_filtered", False)))
            else:
                extra = []
                if it.activity:
                    lo, hi = _day(it.activity[0], tz), _day(it.activity[1], tz)
                    extra.append(f"activity {lo}..{hi}" if lo != hi else f"activity {lo}")
                if it.row.get("git_last_commit_at"):
                    extra.append(f"last commit {_day(it.row['git_last_commit_at'], tz)}")
                lines.append(_folder_line(i, it.row, issued, LEAF_FIND, tz, extra))
        parts = list(head)
        if n:
            parts.append(f"Results {offset + 1}–{offset + n} of {outcome.total} (sort={outcome.sort}):")
            parts.append(ctx.wrap("\n".join(lines)))
        elif outcome.total and offset >= outcome.total:
            parts.append(f"No results on page {outcome.page}; there are {outcome.total} in all.")
        else:
            parts.append("No matching files." if outcome.kind == "file" else "No matching folders.")
        agg = outcome.aggregate
        if agg:
            rows = ", ".join(f"{_clean(str(r['key']))} {r['files']} ({_size(r['bytes'])})" for r in agg["rows"])
            more = f", +{agg['more']} more" if agg.get("more") else ""
            parts.append(f"{agg['by'].replace('_', ' ').capitalize()} (all {agg['files']:,} matching files): "
                         f"{rows}{more}")
        remaining = outcome.total - (offset + n)
        if n and remaining > 0:
            if n < outcome.limit:
                parts.append(f"{remaining} more: call again with limit={n} and page={(offset + n) // n + 1}.")
            else:
                parts.append(f"{remaining} more: call again with page={outcome.page + 1}.")
        if n:
            parts.append(f"Search inside these with {FN_SEARCH} (filters.file_ids or filters.folder), or read "
                         f"one with {FN_READ}.")
        return "\n".join(parts), issued

    n = len(items)
    text, issued = assemble(n)
    while n > 1 and not ctx.fits(text):
        n = max(1, n - max(1, n // 5))
        text, issued = assemble(n)
    shown = items[:n]
    files = [_chip(it.row, ctx) for it in shown if it.kind == "file" and it.row.get("kind") == t.KIND_IMAGE]
    data = {
        "leaf": LEAF_FIND, "query": outcome.query, "kind": outcome.kind, "mode": outcome.mode,
        "mode_reason": outcome.mode_reason, "overview": outcome.overview, "filters": outcome.filters,
        "relaxed": outcome.relaxed, "notes": outcome.notes, "total": outcome.total, "page": outcome.page,
        "limit": outcome.limit, "sort": outcome.sort, "shown": n, "aggregate": outcome.aggregate,
        "items": [
            {
                "kind": it.kind, "fid": it.row.get("cite_id"), "token": make_token(it.row["cite_id"]),
                "name": it.row.get("name"), "rel_path": it.row.get("rel_path"),
                "file_kind": it.row.get("kind") if it.kind == "file" else None,
                "size": it.row.get("size") if it.kind == "file" else None,
                "date": ({"field": it.date[0], "day": _day(it.date[1], tz)} if it.date else None),
                "activity": ([_day(it.activity[0], tz), _day(it.activity[1], tz)] if it.activity else None),
                "score": round(it.score, 6),
            }
            for it in shown
        ],
    }
    return Rendered(text=text, citations=issued.list, files=files[:MAX_IMAGE_CHIPS], data=data)


# ── read ───────────────────────────────────────────────────────────────────

_HEAD_SHARE = 0.35
_TOC_SHARE = 0.25


def _chunk_block(row: dict[str, Any], chunk: dict[str, Any], issued: _Issued) -> str:
    token = issued.add(cite_chunk(row, chunk, LEAF_READ))
    label = _label_for(chunk)
    return f"{token}{' ' + label if label else ''}\n{_clean(chunk.get('text') or '').strip()}"


def _toc_line(e: Any) -> str:
    arg = ", ".join(f'{k}="{_clean(v)}"' for k, v in e.arg.items())
    return f"- {_clean(e.label)} → {arg} (~{e.tokens:,} tokens)"


def render_read(outcome: Any, ctx: RenderContext) -> Rendered:
    tz = _tz_of(outcome)
    row = outcome.file
    ftoken = make_token(row["cite_id"])
    total_tokens = sum(int(c.get("token_est") or 0) for c in outcome.body)
    header_bits = [_clean(row.get("rel_path") or ""), type_of_kind(row.get("kind")), _size(row.get("size"))]
    if row.get("mtime"):
        header_bits.append(f"modified {_day(row['mtime'], tz)}")
    header_bits.append(_n(len(outcome.body), "passage", "passages") + f" (~{total_tokens:,} tokens)")
    head = [f"[Documentation Search · read · {_clean(row.get('name') or '')} {ftoken} — " + " · ".join(header_bits) + "]"]
    if outcome.stale:
        head.append("stale: true")
    for note in outcome.notes:
        head.append(f"Note: {note}")

    def how_to(example: dict[str, str] | None) -> str:
        eg = ""
        if example:
            k, v = next(iter(example.items()))
            eg = f', e.g. {k}="{_clean(v)}"'
        return (f"To read another part, call {FN_READ} with file=\"{ftoken}\" and the argument shown in the "
                f"contents{eg}. Cite passages with the tokens above, copied exactly.")

    def base_issued() -> _Issued:
        issued = _Issued()
        issued.add(cite_file(row, LEAF_READ))
        return issued

    chunks = list(outcome.selected)
    if not outcome.body and outcome.card is not None:
        chunks = [outcome.card]  # metadata only: the card is all there is

    # 1. The selection fits: show it whole.
    issued = base_issued()
    what = f"Showing: {outcome.selection}" if outcome.selection else "Showing: the whole file"
    body = "\n\n".join(_chunk_block(row, c, issued) for c in chunks)
    text = "\n".join(head + [what, ctx.wrap(body) if body else "(nothing to show)",
                             "Cite passages with the tokens above, copied exactly."])
    if ctx.fits(text) or not chunks:
        return Rendered(text=text, citations=issued.list, data=_read_data(outcome, issued, "whole"))

    limit = int(ctx.limit or 0)
    overhead = ctx.tokens("\n".join(head)) + 120

    # 2. An explicit selection too big to show: deliver it in parts.
    if outcome.selection:
        parts: list[list[dict[str, Any]]] = []
        size = 0
        room = max(200, limit - overhead)
        for c in chunks:
            n = ctx.tokens(c.get("text") or "") + 12
            if parts and size + n <= room:
                parts[-1].append(c)
                size += n
            else:
                parts.append([c])
                size = n
        idx = min(outcome.page, len(parts)) - 1
        issued = base_issued()
        body = "\n\n".join(_chunk_block(row, c, issued) for c in parts[idx])
        nav = f"Part {idx + 1} of {len(parts)} of {outcome.selection}."
        if idx + 1 < len(parts):
            nav += f" Call again with page={idx + 2} for the next part."
        text = "\n".join(head + [f"Showing: {outcome.selection} — {nav}", ctx.wrap(body), how_to(None)])
        if not ctx.fits(text):
            # A single chunk larger than the whole budget: its beginning.
            fitted, _ = ctx.fit_lines(body, max(100, limit - overhead))
            text = "\n".join(head + [f"Showing: {outcome.selection} — {nav} (cut to fit)", ctx.wrap(fitted),
                                     how_to(None)])
        return Rendered(text=text, citations=issued.list, data=_read_data(outcome, issued, "part"))

    # 3. The whole file is too long: beginning + contents + matching parts.
    issued = base_issued()
    head_budget = int(limit * _HEAD_SHARE)
    begin: list[str] = []
    used = 0
    for c in chunks:
        block = _chunk_block(row, c, _Issued())
        cost = ctx.tokens(block) + 2
        if begin and used + cost > head_budget:
            break
        if not begin and cost > head_budget:
            fitted, _ = ctx.fit_lines(block, head_budget)
            begin.append(fitted)
            issued.add(cite_chunk(row, c, LEAF_READ))
            used += head_budget
            break
        begin.append(_chunk_block(row, c, issued))
        used += cost
    toc_text, cut = ctx.fit_lines("\n".join(_toc_line(e) for e in outcome.toc), int(limit * _TOC_SHARE),
                                  split_long_line=False)
    toc_note = f"\n({len(outcome.toc) - toc_text.count(chr(10)) - 1} more parts not listed)" if cut else ""

    def assemble(matched: list[Any], iss: _Issued) -> str:
        sections = []
        for e in matched:
            sections.append(f"## {_clean(e.label)}\n" + "\n\n".join(_chunk_block(row, c, iss) for c in e.chunks))
        inner = ["## Beginning", "\n\n".join(begin), "",
                 "## Contents (part → how to ask for it → size)", toc_text + toc_note]
        if outcome.query:
            inner += ["", f"## Parts matching \"{_one_line(_clean(outcome.query))}\""]
            if sections:
                inner.append("\n\n".join(sections))
            elif outcome.ranked:
                inner.append("(Too long to include here: " + ", ".join(
                    f"{_clean(e.label)} ~{e.tokens:,}" for e in outcome.ranked[:5]) + ". Read one on its own.)")
            else:
                inner.append("(No part mentions the query words.)")
        intro = (f"Too long to show whole (~{total_tokens:,} tokens; budget {limit:,}). Below: the beginning, "
                 "the contents with the size of each part"
                 + (", and the parts matching the query." if outcome.query else "."))
        # An example the agent can copy: a structural part (a section, pages,
        # a sheet) rather than a quoted phrase, and one not shown already.
        unshown = [e for e in outcome.toc if e not in matched]
        structural = [e.arg for e in unshown if "around" not in e.arg]
        example = (structural or [e.arg for e in unshown] or [None])[0]
        return "\n".join(head + [intro, ctx.wrap("\n".join(inner)), how_to(example)])

    matched: list[Any] = []
    text = assemble(matched, _copy(issued))
    for e in outcome.ranked[:8]:
        trial_issued = _copy(issued)
        trial = assemble(matched + [e], trial_issued)
        if ctx.fits(trial):
            matched.append(e)
            text = trial
    final = _copy(issued)
    text = assemble(matched, final)
    return Rendered(text=text, citations=final.list, data=_read_data(outcome, final, "envelope"))


def _copy(issued: _Issued) -> _Issued:
    out = _Issued()
    out.items = dict(issued.items)
    return out


def _read_data(outcome: Any, issued: _Issued, shape: str) -> dict[str, Any]:
    row = outcome.file
    return {
        "leaf": LEAF_READ, "fid": row.get("cite_id"), "token": make_token(row["cite_id"]),
        "name": row.get("name"), "rel_path": row.get("rel_path"), "kind": row.get("kind"),
        "selection": outcome.selection, "shape": shape, "stale": outcome.stale, "notes": outcome.notes,
        "toc": [{"label": e.label, "arg": e.arg, "tokens": e.tokens} for e in outcome.toc],
        "passages": [{"token": c.token, "label": c.label, "snippet": c.snippet} for c in issued.list
                     if c.text_hash],
    }


def render_status(message: str, *, code: str | None = None) -> str:
    return f"[Documentation Search · unavailable{' (' + code + ')' if code else ''}] {message}"


__all__ = [
    "FN_FIND",
    "FN_READ",
    "FN_SEARCH",
    "LEAF_FIND",
    "LEAF_READ",
    "LEAF_SEARCH",
    "MAX_IMAGE_CHIPS",
    "RenderContext",
    "Rendered",
    "cite_chunk",
    "cite_file",
    "cite_folder",
    "overview_line",
    "render_find",
    "render_read",
    "render_search",
    "render_status",
]
