"""Rendering query results for the agent (and the CLI), within a budget.

The reasoning agent head-clips every tool result to the profile's
``tool_result.max_tokens``. A clipped search result would lose exactly what
matters — the citation tokens of the passages it cut — so every renderer here
sizes its own output and shrinks it deliberately instead:

- ``search`` drops context expansions first, then shortens snippets, then
  shows fewer results (and says how to page to the rest). The header and every
  token that is printed survive; a token is never cut in half.
- ``read`` shows the whole selection when it fits. A read centred on a
  passage (its token, or ``around`` a phrase) puts that passage first: it is
  shown whole, then as many neighbours as fit, in reading order — or, when it
  cannot fit, a marked excerpt with the rest on the next page. A read of the
  whole file comes back as an envelope (beginning, table of contents with part
  sizes, parts matching the query), and any other selection in parts.
- ``find_files`` lists fewer entries when it must, with a page cursor.

Every result is measured whole — header, data block and footer — against the
limit after it is assembled, never estimated from its parts.

Everything derived from the user's files — names, paths, snippets — is
untrusted content: it goes inside one document-content block per result, and
any ``[doc:`` inside it is defanged, so a file cannot forge a citation or a
block boundary. The renderers also return the tokens they printed, as
:class:`~app.documents.cite.IssuedCitation` records for the citation registry
(only for passages whose text is actually shown, with the text shown as the
snippet), the image thumbnails to attach as file chips, and a
:class:`~app.documents.delivery.DocumentEvidence` record of what the final text
delivered: which passages, whole or in part, and where a read's focus stands.

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
from app.documents.delivery import (
    FOCUS_COMPLETE,
    FOCUS_OMITTED,
    FOCUS_PARTIAL,
    FOCUS_UNRESOLVED,
    OP_READ,
    OP_SEARCH,
    ROLE_BODY,
    ROLE_CONTEXT,
    ROLE_MATCH,
    DocumentEvidence,
    PassageDelivery,
    SourceDelivery,
)
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
    # What ``text`` delivered (search and read only).
    evidence: DocumentEvidence | None = None


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


def cite_chunk(owner: dict[str, Any], chunk: dict[str, Any], leaf: str, *, folder: bool = False,
               snippet: str | None = None) -> IssuedCitation:
    """The registry record of a passage token. ``snippet`` is the text the
    result actually showed of it (a search window, a cut passage); without it
    the passage's own beginning is recorded."""
    loc = chunk.get("locator") or {}
    shown = (chunk.get("text") or "") if snippet is None else snippet
    return IssuedCitation(
        token=make_token(owner["cite_id"], chunk.get("text_hash") or ""), cite_id=owner["cite_id"],
        target="folder" if folder else "file", ref_id=int(owner["id"]),
        text_hash=chunk.get("text_hash"), source_kind=owner.get("source") or "local",
        locator=dict(loc) if isinstance(loc, dict) else {}, label=_label_for(chunk),
        rel_path=owner.get("rel_path") or "", snippet=shown[:SNIPPET_MAX_CHARS],
        leaf=leaf, web_link=None if folder else owner.get("drive_web_link"),
    )


# A contents page's title: "Mục lục", "Table of contents", or "Contents" as a
# line of its own (the word inside a sentence is not a title).
_CONTENTS_TITLE_RE = re.compile(r"\b(?:muc luc|table of contents)\b|(?:^|\n)[ \t]*contents[ \t]*(?:\n|$)")
_CONTENTS_ENTRY_RE = re.compile(
    r"\b(?:phan|chuong|muc|bai|part|chapter|section|unit|lesson|step)\s+(?:\d+|[ivxlc]+)\s*[:.\-–]")
_CONTENTS_LEADER_RE = re.compile(r"\.{4,}\s*\d+")


def _looks_like_contents(chunk: dict[str, Any]) -> bool:
    """A table-of-contents page: a contents title near the top with numbered
    entries under it, or rows of dotted page leaders ("Setup ....... 12").
    It names what the document covers without saying any of it — a file
    matched only there has not been read. Numbered entries alone are not
    enough: a step-by-step procedure ("Step 1: … Step 6: …") is content."""
    text = fold(chunk.get("text") or "")
    if len(_CONTENTS_LEADER_RE.findall(text)) >= 4:
        return True
    entries = len(_CONTENTS_ENTRY_RE.findall(text)) + len(_CONTENTS_LEADER_RE.findall(text))
    return entries >= 3 and _CONTENTS_TITLE_RE.search(text[:160]) is not None


def _substantive(chunk: dict[str, Any]) -> bool:
    """A passage of real content — body text, scanned text, an image's
    caption — rather than a file's or folder's card (name, path, dates) or a
    table of contents."""
    return chunk.get("ctype") in (t.CTYPE_BODY, t.CTYPE_OCR, t.CTYPE_CAPTION) and not _looks_like_contents(chunk)


def _position(chunk: dict[str, Any]) -> str:
    """Where a passage sits, by numbers only ("p. 9", "lines 40–58",
    "slide 5") — safe to print outside the document block, unlike a heading
    or a sheet name, which are the document's own words."""
    loc = chunk.get("locator") or {}
    for key, end, one, many in (("page", "page_end", "p.", "p."), ("slide", None, "slide", "slides"),
                                ("line_start", "line_end", "line", "lines")):
        a = loc.get(key)
        if isinstance(a, int):
            b = loc.get(end) if end else None
            return f"{many} {a}–{b}" if isinstance(b, int) and b > a else f"{one} {a}"
    return ""


def _unmarked(snippet: str) -> str:
    """A shown snippet without the … marks where it was cut."""
    s = snippet
    if s.startswith("… "):
        s = s[2:]
    if s.endswith(" …"):
        s = s[:-2]
    return s


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


@dataclass
class _Shown:
    """A passage a search result printed, and how much of it."""

    owner: dict[str, Any]      # the file (or, for a folder card, the folder) it belongs to
    chunk: dict[str, Any]
    role: str                  # match | context
    complete: bool             # the snippet is the passage's whole text
    order: int                 # the rank of the result it is printed under
    confidence: str | None
    folder: bool = False


def _shown_snippet(chunk: dict[str, Any], tokens: tuple[str, ...], snippet_chars: int) -> tuple[str, bool]:
    """The snippet printed for a passage, and whether it is the whole passage."""
    cleaned = _clean(chunk.get("text") or "")
    return _snippet(cleaned, tokens, snippet_chars), len(_one_line(cleaned)) <= snippet_chars


def _passage_line(hit: Any, issued: _Issued, leaf: str, *, snippet_chars: int, tokens: tuple[str, ...],
                  with_file: bool, order: int) -> tuple[str, _Shown] | None:
    chunk = hit.chunk
    owner = hit.file if hit.file is not None else hit.folder
    if owner is None:
        return None
    text, complete = _shown_snippet(chunk, tokens, snippet_chars)
    token = issued.add(cite_chunk(owner, chunk, leaf, folder=hit.file is None, snippet=_unmarked(text)))
    label = _label_for(chunk)
    # Grouped by folder, a passage names the file it is from.
    prefix = f"{_clean(owner.get('name') or '')} › " if with_file else ""
    shown = _Shown(owner, chunk, ROLE_MATCH, complete, order, hit.confidence, folder=hit.file is None)
    return f"   {prefix}{label + ' ' if label else ''}{token}: {text}", shown


def _search_body(outcome: Any, groups: list[Any], issued: _Issued, *, expand: bool, snippet_chars: int,
                 passages: int, tz: _dt.tzinfo, offset: int) -> tuple[str, list[_Shown]]:
    """The results block, and every passage it prints (matches and context)."""
    tokens = analyze(outcome.query).tokens
    out: list[str] = []
    shown: list[_Shown] = []
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
            placed = _passage_line(hit, issued, LEAF_SEARCH, snippet_chars=snippet_chars, tokens=tokens,
                                   with_file=with_file and hit.file is not None, order=i)
            if placed:
                out.append(placed[0])
                shown.append(placed[1])
            if expand and hit.expansion and hit.file is not None:
                for ctx_chunk in hit.expansion:
                    text, complete = _shown_snippet(ctx_chunk, tokens, snippet_chars)
                    token = issued.add(cite_chunk(hit.file, ctx_chunk, LEAF_SEARCH, snippet=_unmarked(text)))
                    label = _label_for(ctx_chunk)
                    out.append(f"   context {label + ' ' if label else ''}{token}: {text}")
                    shown.append(_Shown(hit.file, ctx_chunk, ROLE_CONTEXT, complete, i, None))
            if hit.also_in:
                also = ", ".join(
                    f"{_clean(o.get('rel_path') or '')} {issued.add(cite_file(o, LEAF_SEARCH))}"
                    for o in hit.also_in[:3])
                more = f" (+{len(hit.also_in) - 3} more)" if len(hit.also_in) > 3 else ""
                out.append(f"   same text also in: {also}{more}")
    return "\n".join(out), shown


def _shown_files(shown: list[_Shown]) -> list[_Shown]:
    """The first shown passage of each file, in result order (folder cards
    are not files)."""
    seen: dict[str, _Shown] = {}
    for s in shown:
        if not s.folder:
            seen.setdefault(s.owner["cite_id"], s)
    return list(seen.values())


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

    def assemble(n: int, expand: bool, chars: int, passages: int) -> tuple[str, _Issued, list[_Shown]]:
        issued = _Issued()
        shown = groups[:n]
        if not shown:
            if outcome.total and offset >= outcome.total:
                summary = f"No results on page {outcome.page}; there are {outcome.total} in all."
            else:
                summary = "No results. Try other words, fewer filters, or documentation_search__find_files by name."
            return "\n".join(head + [summary]), issued, []
        summary = (f"Results {offset + 1}–{offset + len(shown)} of {outcome.total} "
                   f"(grouped by {outcome.group_by}; best first):")
        body, printed = _search_body(outcome, shown, issued, expand=expand, snippet_chars=chars,
                                     passages=passages, tz=tz, offset=offset)
        foot = [_search_cite_line(len(_shown_files(printed)))]
        more = _search_more(outcome, offset, len(shown))
        if more:
            foot.append(more[0])
        text = "\n".join(head + [summary, ctx.wrap(body)] + foot)
        return text, issued, printed

    levels = [(True, _SNIPPET_STEPS[0], 2), (False, _SNIPPET_STEPS[0], 2), (False, _SNIPPET_STEPS[1], 2),
              (False, _SNIPPET_STEPS[1], 1), (False, _SNIPPET_STEPS[2], 1)]
    n = len(groups)
    text, issued, printed, level = "", _Issued(), [], levels[0]
    shrunk = False
    for level in levels:
        text, issued, printed = assemble(n, *level)
        if ctx.fits(text):
            break
        shrunk = True
    else:
        while n > 1 and not ctx.fits(text):
            n -= 1
            text, issued, printed = assemble(n, *level)
        chars = level[1]
        while not ctx.fits(text) and chars > 40:
            chars = max(40, chars // 2)
            text, issued, printed = assemble(n, False, chars, 1)
        if not ctx.fits(text):
            # Not even one result fits beside the header: say so, within the
            # limit, rather than send something the agent's clamp would cut.
            n = 0
            text, issued, printed = _search_too_small(head, outcome, ctx), _Issued(), []
    shown = groups[:n]
    files = [_chip(g.file, ctx) for g in shown if g.file is not None and g.file.get("kind") == t.KIND_IMAGE]
    more = _search_more(outcome, offset, n) if n else None
    evidence = _search_evidence(printed, shown, offset, ctx.tokens(text), truncated=shrunk or n < len(groups),
                                continuation=more[1] if more else None)
    data = _search_data(outcome, shown, issued, tz, printed)
    data["delivery"] = evidence.public()
    return Rendered(text=text, citations=issued.list, files=files[:MAX_IMAGE_CHIPS], data=data,
                    evidence=evidence)


def _search_cite_line(files: int) -> str:
    """The footer's instructions. With passages from several files the agent
    is asked to weigh each one before answering — the failure this exists
    for is an answer drawn from one of two relevant guides, the other left
    unread though it ranked first."""
    cite = ("Cite every claim with the [doc:…] token printed next to the passage it comes from, copied "
            "exactly. ")
    if files > 1:
        return cite + (f"These results come from {files} files: before answering, assess each relevant one "
                       f"— read a passage in full with {FN_READ} (file=its passage token) — and attribute "
                       "any difference between them to the file it comes from.")
    return cite + f"Read more of a file with {FN_READ} (file=its token)."


def _search_more(outcome: Any, offset: int, shown: int) -> tuple[str, dict[str, int]] | None:
    """The paging line for the results not shown, and the same as arguments."""
    remaining = outcome.total - (offset + shown)
    if remaining <= 0 or not shown:
        return None
    if shown < outcome.top_k:
        nxt = (offset + shown) // shown + 1
        return (f"{remaining} more: call again with top_k={shown} and page={nxt}.",
                {"page": nxt, "top_k": shown})
    return f"{remaining} more: call again with page={outcome.page + 1}.", {"page": outcome.page + 1}


def _search_too_small(head: list[str], outcome: Any, ctx: RenderContext) -> str:
    note = (f"{outcome.total} result(s) found, but the reply budget is too small to show any of them: "
            "call again with top_k=1, or narrow the search.")
    return _too_small(head, [note], ctx)


def _too_small(head: list[str], lines: list[str], ctx: RenderContext) -> str:
    """A result that can show nothing, fitted to the limit: the first header
    line, then what to do, then the rest of the header — so a long header is
    what gets cut, never the explanation."""
    text = "\n".join(head[:1] + lines + head[1:])
    return text if ctx.fits(text) else _fit_plain(text, ctx)


def _fit_plain(text: str, ctx: RenderContext) -> str:
    """``text`` — headers and notes, no document block and no token — cut to
    the limit. The last resort of a budget smaller than a header."""
    limit = int(ctx.limit or 0)
    fitted, _ = ctx.fit_lines(text, limit)
    while fitted and not ctx.fits(fitted):
        fitted = ctx.cut_tokens(fitted, max(0, ctx.tokens(fitted) - 8)).rstrip()
    return fitted


def _search_evidence(printed: list[_Shown], shown: list[Any], offset: int, rendered_tokens: int, *,
                     truncated: bool, continuation: dict[str, int] | None) -> DocumentEvidence:
    """What a search text delivered: every passage it printed (each token
    once, whole if any copy was whole), and the files its results name in
    rank order — a file result itself, or, for a folder result, the files of
    the passages it printed (grouped by chunk, each result is one file)."""
    passages: dict[str, PassageDelivery] = {}
    for s in printed:
        token = make_token(s.owner["cite_id"], s.chunk.get("text_hash") or "")
        prev = passages.get(token)
        # A passage printed twice (a result's second match is often the first
        # match's context too) is one delivery: whole if either copy was, a
        # match if either was one.
        match = s if s.role == ROLE_MATCH else None
        if prev is not None and prev.role == ROLE_MATCH:
            match = None
        passages[token] = PassageDelivery(
            token=token, fid=s.owner["cite_id"],
            role=ROLE_MATCH if (match or (prev and prev.role == ROLE_MATCH)) else s.role,
            complete=s.complete or bool(prev and prev.complete),
            substantive=_substantive(s.chunk) and not s.folder,
            order=min(s.order, prev.order) if prev else s.order,
            confidence=match.confidence if match else (prev.confidence if prev else s.confidence),
            position=_position(s.chunk),
        )
    sources: dict[str, SourceDelivery] = {}
    for j, g in enumerate(shown):
        rank = offset + j + 1
        if g.kind == "file" and g.file is not None:
            owners = [g.file]
        else:
            owners = [s.owner for s in printed if s.order == rank and not s.folder]
        for f in owners:
            sources.setdefault(f["cite_id"], SourceDelivery(fid=f["cite_id"], token=make_token(f["cite_id"]),
                                                            order=rank))
    return DocumentEvidence(op=OP_SEARCH, passages=list(passages.values()), sources=list(sources.values()),
                            rendered_tokens=rendered_tokens, truncated=truncated, continuation=continuation)


def _search_data(outcome: Any, shown: list[Any], issued: _Issued, tz: _dt.tzinfo,
                 printed: list[_Shown] | None = None) -> dict[str, Any]:
    # ``visible``: whether the passage is in the text (a result lists up to
    # two passages; the text may print fewer when the budget is tight).
    visible = {make_token(s.owner["cite_id"], s.chunk.get("text_hash") or "") for s in (printed or [])}
    items = []
    for g in shown:
        owner = g.file if g.file is not None else g.folder
        passages = []
        for h in g.passages:
            o = h.file if h.file is not None else h.folder
            if o is None:
                continue
            token = make_token(o["cite_id"], h.chunk.get("text_hash") or "")
            passages.append({
                "token": token,
                "label": _label_for(h.chunk),
                "file": o.get("rel_path"),
                "snippet": _one_line(h.chunk.get("text") or "")[:SNIPPET_MAX_CHARS],
                "score": round(h.score, 6),
                "confidence": h.confidence,
                "visible": token in visible,
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
    remaining = outcome.total - (offset + n)
    data["delivery"] = {
        "v": 1, "rendered_tokens": ctx.tokens(text), "truncated": n < len(items), "passages": [],
        "continuation": (None if not n or remaining <= 0 else
                         {"page": outcome.page + 1} if n >= outcome.limit else
                         {"page": (offset + n) // n + 1, "limit": n}),
    }
    return Rendered(text=text, citations=issued.list, files=files[:MAX_IMAGE_CHIPS], data=data)


# ── read ───────────────────────────────────────────────────────────────────

_HEAD_SHARE = 0.35
_TOC_SHARE = 0.25
# Kept free when a part is packed by adding up its passages' sizes, for what
# adding cannot see (a join that tokenizes differently); every part is still
# measured whole before it goes out.
_PACK_MARGIN = 16
# The least room worth showing a piece of a passage in; below it the result
# says the budget is too small rather than print a few words.
_MIN_PIECE_TOKENS = 24
_CITE_LINE = "Cite passages with the tokens above, copied exactly."


@dataclass
class _Piece:
    """A passage as a read shows it: whole, or one piece of it when it had to
    be cut or split (``first`` / ``last``: which ends of it the piece holds)."""

    chunk: dict[str, Any]
    text: str
    complete: bool = True
    first: bool = True
    last: bool = True


def _whole(chunk: dict[str, Any]) -> _Piece:
    return _Piece(chunk, _clean(chunk.get("text") or "").strip())


def _piece_block(row: dict[str, Any], p: _Piece) -> str:
    """A passage's token line and text; a piece says which part of the
    passage it is, so a cut passage never reads as a whole one."""
    token = make_token(row["cite_id"], p.chunk.get("text_hash") or "")
    label = _label_for(p.chunk)
    line = f"{token}{' ' + label if label else ''}"
    if p.complete:
        return f"{line}\n{p.text}"
    if p.first:
        return f"{line} (beginning of the passage)\n{p.text} …"
    if p.last:
        return f"{line} (end of the passage)\n… {p.text}"
    return f"{line} (continued)\n… {p.text} …"


def _read_text(ctx: RenderContext, head: list[str], showing: str, row: dict[str, Any], pieces: list[_Piece],
               tail: list[str]) -> str:
    body = "\n\n".join(_piece_block(row, p) for p in pieces)
    return "\n".join(head + [showing, ctx.wrap(body) if body else "(nothing to show)"] + tail)


def _piece_room(ctx: RenderContext, render_one: Callable[[_Piece], str], chunk: dict[str, Any]) -> int | None:
    """Tokens of text one piece of ``chunk`` may hold in a result that
    ``render_one`` lays out around it; None when the budget leaves no useful
    room."""
    base = ctx.tokens(render_one(_Piece(chunk, "", False, first=False, last=False)))
    room = int(ctx.limit or 0) - base - _PACK_MARGIN
    return room if room >= _MIN_PIECE_TOKENS else None


def _split(ctx: RenderContext, chunk: dict[str, Any], room: int) -> list[_Piece]:
    """A passage too long for one result, as consecutive pieces of at most
    ``room`` tokens of text each, cut at a word break where one is close."""
    text = _clean(chunk.get("text") or "").strip()
    texts: list[str] = []
    rest = text
    while rest:
        if ctx.tokens(rest) <= room:
            texts.append(rest)
            break
        cut = ctx.cut_tokens(rest, room)
        if not cut.strip():
            # One word longer than the room (a URL, a hash): cut inside it.
            cut = rest[: max(1, room // 3)]
        texts.append(cut.rstrip())
        rest = rest[len(cut):].lstrip()
    if len(texts) <= 1:
        return [_Piece(chunk, text)]
    return [_Piece(chunk, s, False, first=i == 0, last=i == len(texts) - 1) for i, s in enumerate(texts)]


def _shrink_to_fit(ctx: RenderContext, pieces: list[_Piece],
                   render: Callable[[list[_Piece]], str]) -> tuple[str, list[_Piece]]:
    """The text of ``pieces``, cut to the limit if it still overflows once
    assembled: the last piece listed is shortened (then dropped) first, so a
    caller lists what matters most first. What comes back is exactly what the
    text shows."""
    pieces = list(pieces)
    text = render(pieces)
    while pieces and not ctx.fits(text):
        last = pieces[-1]
        keep = ctx.tokens(last.text) - (ctx.tokens(text) - int(ctx.limit or 0)) - 4
        if keep < _MIN_PIECE_TOKENS:
            pieces.pop()
        else:
            pieces[-1] = _Piece(last.chunk, ctx.cut_tokens(last.text, keep).rstrip(), False,
                                first=last.first, last=False)
        text = render(pieces)
    if not ctx.fits(text):
        text, pieces = _fit_plain(text, ctx), []
    return text, pieces


def _neighbour(chunk: dict[str, Any], which: str) -> str:
    where = _position(chunk)
    return f"the passage {which} it" + (f" ({where})" if where else "")


def _left_out(items: list[str], next_page: int) -> str:
    listed = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    return f"Left out here to fit the budget: {listed} — call again with page={next_page} for the next part."


def _next_part(page: int) -> str:
    return f"Call again with page={page} for the next part."


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

    chunks = list(outcome.selected)
    if not outcome.body and outcome.card is not None:
        chunks = [outcome.card]  # metadata only: the card is all there is

    # 1. The selection fits: show it whole.
    pieces = [_whole(c) for c in chunks]
    what = f"Showing: {outcome.selection}" if outcome.selection else "Showing: the whole file"
    text = _read_text(ctx, head, what, row, pieces, [_CITE_LINE])
    if ctx.fits(text):
        return _read_result(outcome, ctx, text, pieces, shape="whole")
    if not chunks:
        return _read_result(outcome, ctx, _fit_plain(text, ctx), [], shape="whole", truncated=True)

    # 2. A read centred on a passage: that passage first.
    fidx = next((i for i, c in enumerate(chunks)
                 if outcome.focus_id is not None and int(c["id"]) == int(outcome.focus_id)), None)
    if fidx is not None:
        return _render_focus(outcome, ctx, head, chunks, fidx)

    # 3. Any other selection too big to show: in parts.
    if outcome.selection:
        return _render_parts(outcome, ctx, head, chunks, how_to(None))

    # 4. The whole file is too long: beginning + contents + matching parts.
    return _render_envelope(outcome, ctx, head, chunks, how_to, total_tokens)


def _render_focus(outcome: Any, ctx: RenderContext, head: list[str], chunks: list[dict[str, Any]],
                  fidx: int) -> Rendered:
    """A read centred on one passage (its token, or ``around`` a phrase) that
    does not fit whole with its neighbours.

    The first page is built around the passage: it goes in first, whole when
    it fits, then the passage after it and the one before it, each only if
    it fits whole — shown in reading order. A neighbour that could not go in,
    and the rest of a passage too long for any page, follow on later pages,
    and the first page says what was left out and how to get it. So the
    passage asked for is never pushed out by a long one before it.
    """
    row = outcome.file
    order = {int(c["id"]): i for i, c in enumerate(chunks)}
    focus = chunks[fidx]
    neighbours = [(nb, which) for nb, which in ((chunks[fidx + 1] if fidx + 1 < len(chunks) else None, "after"),
                                               (chunks[fidx - 1] if fidx > 0 else None, "before"))
                  if nb is not None]
    sel = outcome.selection or "the passage asked for"

    def text_of(pieces: list[_Piece], part: int, total: int, notes: list[str]) -> str:
        showing = f"Showing: {sel}" + (" — the passage asked for first" if part == 1 else "") + (
            f"; part {part} of {total}." if total > 1 else ".")
        ordered = sorted(pieces, key=lambda p: order[int(p.chunk["id"])])
        return _read_text(ctx, head, showing, row, ordered, notes + [_CITE_LINE])

    # Every layout is measured with the longest note it could carry, so what
    # fits here still fits once the real note is written.
    worst_first = [_left_out(["the rest of the passage asked for"]
                             + [_neighbour(nb, w) for nb, w in neighbours], 99)]
    worst_later = [_next_part(99)]

    parts: list[list[_Piece]] = []
    whole = _whole(focus)
    if ctx.fits(text_of([whole], 1, 99, worst_first)):
        first = [whole]
        for nb, _which in neighbours:
            trial = first + [_whole(nb)]
            if ctx.fits(text_of(trial, 1, 99, worst_first)):
                first = trial
        parts.append(first)
    else:
        room = _piece_room(ctx, lambda p: text_of([p], 1, 99, worst_first), focus)
        if room is None:
            note = "The reply budget is too small to show the passage asked for; ask with a larger budget."
            text = _too_small(head, [f"Showing: {sel}.", note], ctx)
            return _read_result(outcome, ctx, text, [], shape="focus", truncated=True,
                                continuation=None, focus_status=FOCUS_OMITTED)
        parts += [[p] for p in _split(ctx, focus, room)]
    placed = {int(p.chunk["id"]) for part in parts for p in part if p.complete}
    for nb, _which in sorted(neighbours, key=lambda x: order[int(x[0]["id"])]):
        if int(nb["id"]) in placed:
            continue
        if ctx.fits(text_of([_whole(nb)], 99, 99, worst_later)):
            parts.append([_whole(nb)])
            continue
        room = _piece_room(ctx, lambda p: text_of([p], 99, 99, worst_later), nb)
        # A later page's layout is never larger than the first's, so a room
        # the focus had is there for its neighbour; should it not be, the
        # neighbour is left out rather than promised on a page that is empty.
        if room:
            parts += [[p] for p in _split(ctx, nb, room)]

    total = len(parts)
    idx = min(outcome.page, total) - 1
    notes: list[str] = []
    if idx == 0 and total > 1:
        # Only what the later pages really hold is announced.
        later = {int(p.chunk["id"]) for part in parts[1:] for p in part}
        left = [] if parts[0][0].complete else ["the rest of the passage asked for"]
        left += [_neighbour(nb, w) for nb, w in neighbours if int(nb["id"]) in later]
        notes.append(_left_out(left, 2) if left else _next_part(2))
    elif idx + 1 < total:
        notes.append(_next_part(idx + 2))
    # The focus is listed first, so a last-resort cut takes a neighbour first.
    shown = sorted(parts[idx], key=lambda p: 0 if int(p.chunk["id"]) == int(focus["id"]) else 1)
    text, shown = _shrink_to_fit(ctx, shown, lambda ps: text_of(ps, idx + 1, total, notes))
    return _read_result(outcome, ctx, text, shown, shape="focus", truncated=total > 1,
                        continuation={"page": idx + 2} if idx + 1 < total else None)


def _render_parts(outcome: Any, ctx: RenderContext, head: list[str], chunks: list[dict[str, Any]],
                  how_to: str) -> Rendered:
    """An explicit selection (pages, a section, a sheet…) too long for one
    result, in parts of whole passages; ``page`` picks the part. Parts are
    packed by the passages' measured sizes, and a passage longer than a whole
    part is split across consecutive parts rather than cut short."""
    row = outcome.file
    sel = outcome.selection

    def text_of(pieces: list[_Piece], part: int, total: int) -> str:
        nav = f"Part {part} of {total} of {sel}."
        if part < total:
            nav += f" Call again with page={part + 1} for the next part."
        return _read_text(ctx, head, f"Showing: {sel} — {nav}", row, pieces, [how_to])

    probe = _whole(chunks[0])
    base = ctx.tokens(text_of([probe], 99, 99)) - ctx.tokens(_piece_block(row, probe))
    room = int(ctx.limit or 0) - base - _PACK_MARGIN

    def too_small() -> Rendered:
        # Parts that could show nothing would still point to "the next
        # part" forever; say so once instead, with nothing to continue.
        note = "The reply budget is too small to show any part of this selection."
        return _read_result(outcome, ctx, _too_small(head, [f"Showing: {sel}.", note], ctx), [],
                            shape="part", truncated=True)

    if room < _MIN_PIECE_TOKENS:
        return too_small()
    parts: list[list[_Piece]] = []
    current: list[_Piece] = []
    size = 0
    for c in chunks:
        p = _whole(c)
        cost = ctx.tokens(_piece_block(row, p)) + 2
        if current and size + cost > room:
            parts.append(current)
            current, size = [], 0
        if not current and cost > room:
            proom = _piece_room(ctx, lambda q: text_of([q], 99, 99), c)
            if proom is None:
                return too_small()
            parts += [[q] for q in _split(ctx, c, proom)]
            continue
        current.append(p)
        size += cost
    if current:
        parts.append(current)
    total = len(parts)
    idx = min(outcome.page, total) - 1
    text, shown = _shrink_to_fit(ctx, parts[idx], lambda ps: text_of(ps, idx + 1, total))
    return _read_result(outcome, ctx, text, shown, shape="part", truncated=total > 1,
                        continuation={"page": idx + 2} if idx + 1 < total else None)


def _render_envelope(outcome: Any, ctx: RenderContext, head: list[str], chunks: list[dict[str, Any]],
                     how_to: Callable[[dict[str, str] | None], str], total_tokens: int) -> Rendered:
    """The whole file, too long to show: its beginning, the table of contents
    with the size of each part, and the parts matching the query."""
    row = outcome.file
    limit = int(ctx.limit or 0)
    head_budget = int(limit * _HEAD_SHARE)
    begin: list[_Piece] = []
    used = 0
    for c in chunks:
        p = _whole(c)
        cost = ctx.tokens(_piece_block(row, p)) + 2
        if begin and used + cost > head_budget:
            break
        if not begin and cost > head_budget:
            # The first passage alone is longer than the beginning's share.
            room = head_budget - ctx.tokens(_piece_block(row, _Piece(c, "", False))) - 4
            if room >= _MIN_PIECE_TOKENS:
                begin.append(_Piece(c, ctx.cut_tokens(p.text, room).rstrip(), False, first=True, last=False))
            break
        begin.append(p)
        used += cost

    def contents(cap: int) -> str:
        if cap <= 0:
            return "(not listed: the reply budget is too small)"
        listed, cut = ctx.fit_lines("\n".join(_toc_line(e) for e in outcome.toc), cap, split_long_line=False)
        return listed + (f"\n({len(outcome.toc) - listed.count(chr(10)) - 1} more parts not listed)" if cut else "")

    def assemble(start: list[_Piece], toc: str, matched: list[Any]) -> str:
        sections = [f"## {_clean(e.label)}\n" + "\n\n".join(_piece_block(row, _whole(c)) for c in e.chunks)
                    for e in matched]
        inner = ["## Beginning", "\n\n".join(_piece_block(row, p) for p in start), "",
                 "## Contents (part → how to ask for it → size)", toc]
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

    cap = int(limit * _TOC_SHARE)
    toc = contents(cap)
    text = assemble(begin, toc, [])
    # The shares cannot see a long header: shrink the beginning, then the
    # contents, until the envelope itself fits.
    while not ctx.fits(text) and begin:
        begin.pop()
        text = assemble(begin, toc, [])
    while not ctx.fits(text) and cap > 0:
        cap //= 2
        toc = contents(cap)
        text = assemble(begin, toc, [])
    if not ctx.fits(text):
        note = "The reply budget is too small to show any part of this file."
        return _read_result(outcome, ctx, _too_small(head, [note], ctx), [], shape="envelope", truncated=True)
    matched: list[Any] = []
    for e in outcome.ranked[:8]:
        trial = assemble(begin, toc, matched + [e])
        if ctx.fits(trial):
            matched.append(e)
            text = trial
    pieces = begin + [_whole(c) for e in matched for c in e.chunks]
    return _read_result(outcome, ctx, text, pieces, shape="envelope", truncated=True)


def _read_result(outcome: Any, ctx: RenderContext, text: str, pieces: list[_Piece], *, shape: str,
                 truncated: bool = False, continuation: dict[str, int] | None = None,
                 focus_status: str | None = None) -> Rendered:
    """A read's result from its final text and the pieces that text shows:
    the citations (the file, and each passage shown, with the text shown as
    its snippet) and the delivery record — which passages are whole, and
    where the passage a read was centred on stands."""
    row = outcome.file
    issued = _Issued()
    issued.add(cite_file(row, LEAF_READ))
    delivered: dict[str, PassageDelivery] = {}
    for p in pieces:
        token = issued.add(cite_chunk(row, p.chunk, LEAF_READ, snippet=p.text))
        prev = delivered.get(token)
        if prev is None or (p.complete and not prev.complete):
            delivered[token] = PassageDelivery(token=token, fid=row["cite_id"], role=ROLE_BODY,
                                               complete=p.complete, substantive=_substantive(p.chunk),
                                               position=_position(p.chunk))
    focus: str | None = None
    status = focus_status
    if outcome.focus_unresolved:
        focus, status = outcome.focus_unresolved, FOCUS_UNRESOLVED
    elif outcome.focus_id is not None:
        chunk = next((c for c in outcome.selected if int(c["id"]) == int(outcome.focus_id)), None)
        if chunk is not None:
            focus = make_token(row["cite_id"], chunk.get("text_hash") or "")
            if status is None:
                got = delivered.get(focus)
                status = FOCUS_COMPLETE if got and got.complete else FOCUS_PARTIAL if got else FOCUS_OMITTED
    evidence = DocumentEvidence(
        op=OP_READ, passages=list(delivered.values()),
        sources=[SourceDelivery(fid=row["cite_id"], token=make_token(row["cite_id"]), order=1)],
        rendered_tokens=ctx.tokens(text), truncated=truncated, continuation=continuation,
        fid=row["cite_id"], focus=focus, focus_status=status, stale=bool(outcome.stale),
        metadata_only=not outcome.body, shape=shape,
    )
    data = _read_data(outcome, issued, shape)
    data["delivery"] = evidence.public()
    return Rendered(text=text, citations=issued.list, data=data, evidence=evidence)


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
