"""``read``: the text of one indexed file, rebuilt from its chunks.

The reader never opens the user's file — it reassembles the text the index
already holds, chunk by chunk in reading order, so every passage it returns
carries the exact citation token of the chunk it came from. It selects by the
locators the extractors recorded: PDF pages, text lines, a legal article or a
heading ("Điều 203", "Budget"), a spreadsheet sheet and rows, a slide, or the
passage around a quoted phrase or a chunk token.

When nothing is asked and the file is long, the renderer shows an envelope —
the head, a table of contents keyed on the file's own structure (articles,
headings, sheets, slides, pages or line ranges) with the size of each part,
and the parts matching ``query`` — so the agent can ask for exactly what it
needs next.

If the file changed on disk since it was indexed, the result says so
(``stale``) and the file is queued for re-indexing ahead of everything else.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from app.userdocs import types as t
from app.userdocs.cite import parse_tokens
from app.userdocs.query.terms import analyze, matches_text
from app.userdocs.textnorm import fold, normalize_for_match

# A table-of-contents entry for plain text or pages groups about this much.
_PART_TOKENS = 800
_MAX_TOC_ENTRIES = 400

_LEGAL_SECTION_RE = re.compile(
    r"(?:(?:khoản|khoan|clause)\s+(?P<cl1>\d+)\s*[,;]?\s*(?:của\s+)?)?"
    r"(?:điều|dieu|article|art\.?|section)\s*(?P<art>\d+[a-z]?)"
    r"(?:\s*[,;.]?\s*(?:khoản|khoan|clause|\()\s*(?P<cl2>\d+)\)?)?",
    re.IGNORECASE,
)


class ReadError(Exception):
    """A ``file`` or locator the agent must correct; ``candidates`` are the
    closest valid values."""

    def __init__(self, code: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []


@dataclass
class TocEntry:
    label: str
    arg: dict[str, str]                  # how to ask for it: {"section": "Điều 12"}
    chunks: list[dict[str, Any]]
    tokens: int


@dataclass
class ReadOutcome:
    file: dict[str, Any]
    card: dict[str, Any] | None
    body: list[dict[str, Any]]           # every content chunk, in reading order
    selected: list[dict[str, Any]]
    selection: str | None                # "pages=3-4", or None for the whole file
    toc: list[TocEntry]
    ranked: list[TocEntry]               # entries matching ``query``, best first
    query: str | None
    stale: bool = False
    notes: list[str] = field(default_factory=list)
    focus_id: int | None = None          # the chunk a token or ``around`` pointed at
    page: int = 1
    tz_name: str = ""
    tz: Any = None


# ── resolving the file ─────────────────────────────────────────────────────


def resolve_file(engine: Any, ref: str) -> tuple[dict[str, Any], str | None]:
    """The file ``ref`` names, and the chunk part of a token if it had one.

    ``ref`` may be a citation token, a bare file id, a path relative to the
    indexed folder (or an absolute path inside it), or a file name. Only this
    profile's index is consulted, so another profile's id is simply unknown.
    """
    db = engine.db
    raw = (ref or "").strip()
    if not raw:
        raise ReadError("MissingFile", "Pass `file`: a [ud:…] token, a file id, a path or a file name.")
    if raw.lower().startswith("research:"):
        raise ReadError("NotAvailable", "Research results cannot be read yet.")
    toks = parse_tokens(raw) if "ud:" in raw.lower() else []
    cite, c8 = (toks[0]["cite_id"], toks[0]["c8"]) if toks else (None, None)
    if cite is None and re.fullmatch(r"[0-9a-hjkmnp-tv-z]{8}", raw.lower()):
        cite = raw.lower()
    if cite is not None:
        row = db.file_by_cite(cite)
        if row is not None and row.get("status") not in ("missing", "tombstone"):
            return row, c8
        if db.folder_by_cite(cite) is not None:
            raise ReadError("IsAFolder", "That token names a folder. List its files with "
                            "user_documents__find_files and filters.folder, then read one of them.")
        raise ReadError("NotFound", f"No indexed file has the id {cite!r}.")

    path = raw.replace("\\", "/")
    if engine.root and os.path.isabs(raw):
        try:
            path = os.path.relpath(raw, engine.root).replace(os.sep, "/")
        except ValueError:
            path = raw
    path = path.strip("/")
    visible = "status NOT IN ('missing', 'tombstone')"
    rows = db.read_sql(f"SELECT * FROM files WHERE rel_path = ? AND {visible}", (path,), table="files")
    if not rows:
        rows = db.read_sql(
            f"SELECT * FROM files WHERE lower(rel_path) = lower(?) AND {visible}", (path,), table="files")
    if not rows:
        name = path.rsplit("/", 1)[-1]
        rows = db.read_sql(f"SELECT * FROM files WHERE name_folded = ? AND {visible} LIMIT 20",
                           (fold(name),), table="files")
    if len(rows) == 1:
        return rows[0], None
    if len(rows) > 1:
        raise ReadError("AmbiguousFile", f"{len(rows)} files are named {raw!r}; pass one of these tokens.",
                        [f"[ud:{r['cite_id']}] {r['rel_path']}" for r in rows[:10]])
    stem = fold(path.rsplit("/", 1)[-1])
    close = db.read_sql(
        f"SELECT cite_id, rel_path FROM files WHERE name_folded LIKE ? AND {visible} LIMIT 10",
        (f"%{stem[:12]}%",)) if stem else []
    raise ReadError("NotFound", f"No indexed file matches {raw!r}.",
                    [f"[ud:{r['cite_id']}] {r['rel_path']}" for r in close])


# ── locators ───────────────────────────────────────────────────────────────


def parse_ranges(value: Any, name: str) -> list[tuple[int, int]]:
    """"3", "3-5", "3,7-9" (1-based, inclusive) → [(3, 3), (3, 5), …]."""
    if isinstance(value, int):
        return [(value, value)]
    out: list[tuple[int, int]] = []
    for part in str(value).replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:[-–](\d+))?", part)
        if not m:
            raise ReadError("InvalidLocator", f"{name}={value!r}: use a number or a range like 3-5")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        out.append((min(lo, hi), max(lo, hi)))
    if not out:
        raise ReadError("InvalidLocator", f"{name} is empty")
    return out


def _span(loc: dict[str, Any], start: str, end: str) -> tuple[int, int] | None:
    a = loc.get(start)
    if not isinstance(a, int):
        return None
    b = loc.get(end)
    return a, b if isinstance(b, int) and b >= a else a


def _overlaps(span: tuple[int, int] | None, ranges: list[tuple[int, int]]) -> bool:
    return span is not None and any(span[0] <= hi and lo <= span[1] for lo, hi in ranges)


def _rows_of(loc: dict[str, Any]) -> tuple[int, int] | None:
    rows = loc.get("rows")
    if isinstance(rows, (list, tuple)) and len(rows) == 2:
        return int(rows[0]), int(rows[1])
    rng = loc.get("range")
    if isinstance(rng, str):
        nums = [int(n) for n in re.findall(r"\d+", rng)]
        if nums:
            return min(nums), max(nums)
    return None


def legal_key(section: str) -> str | None:
    """"Điều 203" → "art:203"; "khoản 2 Điều 203" / "Article 5(2)" →
    "art:203/cl:2" — the ``section_key`` the chunker writes."""
    m = _LEGAL_SECTION_RE.search(section or "")
    if not m:
        return None
    clause = m.group("cl1") or m.group("cl2")
    return f"art:{m.group('art').lower()}" + (f"/cl:{clause}" if clause else "")


def _heading_path(chunk: dict[str, Any]) -> list[str]:
    heading = (chunk.get("locator") or {}).get("heading")
    if isinstance(heading, list) and heading:
        return [str(h) for h in heading]
    crumb = chunk.get("heading") or ""
    return [p.strip() for p in crumb.split("›") if p.strip()] if crumb else []


def _is_vietnamese(chunks: list[dict[str, Any]]) -> bool:
    for c in chunks[:50]:
        if any(w in " ".join(_heading_path(c)) for w in ("Điều", "Chương", "Khoản")):
            return True
    return False


# ── the table of contents ──────────────────────────────────────────────────


def _tokens(chunks: list[dict[str, Any]]) -> int:
    return sum(int(c.get("token_est") or 0) for c in chunks)


def _runs(chunks: list[dict[str, Any]], key) -> list[tuple[Any, list[dict[str, Any]]]]:
    out: list[tuple[Any, list[dict[str, Any]]]] = []
    for c in chunks:
        k = key(c)
        if out and out[-1][0] == k:
            out[-1][1].append(c)
        else:
            out.append((k, [c]))
    return out


def _sized(chunks: list[dict[str, Any]], budget: int = _PART_TOKENS) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    size = 0
    for c in chunks:
        n = int(c.get("token_est") or 0)
        if groups and size + n <= budget:
            groups[-1].append(c)
            size += n
        else:
            groups.append([c])
            size = n
    return groups


def build_toc(body: list[dict[str, Any]]) -> list[TocEntry]:
    """The file's parts, keyed on its own structure: legal articles, then
    headings, sheets, slides, pages, line ranges — whichever it has first."""
    if not body:
        return []
    locs = [c.get("locator") or {} for c in body]
    vi = _is_vietnamese(body)
    if any(c.get("section_key") for c in body):
        def art(c: dict[str, Any]) -> str | None:
            key = c.get("section_key") or ""
            return key.split("/")[0] if key else None
        entries = []
        for key, run in _runs(body, art):
            if key:
                num = key.split(":", 1)[1]
                word = "Điều" if vi else "Article"
                tail = _heading_path(run[0])[-1] if _heading_path(run[0]) else ""
                label = f"{word} {num}" + (f" — {tail}" if tail and not fold(tail).startswith(fold(word)) else "")
                entries.append(TocEntry(label, {"section": f"{word} {num}"}, run, _tokens(run)))
            else:
                entries.append(TocEntry(_first_words(run), {"around": _first_words(run)}, run, _tokens(run)))
        return entries[:_MAX_TOC_ENTRIES]
    paths = [_heading_path(c) for c in body]
    if any(paths):
        # The top heading when the file has several; the second level when a
        # single title heads everything.
        tops = {p[0] for p in paths if p}
        level = 1 if len(tops) == 1 and sum(1 for p in paths if len(p) > 1) >= len(paths) // 2 else 0
        entries = []
        for key, run in _runs(body, lambda c: tuple(_heading_path(c)[: level + 1])):
            title = key[-1] if key else ""
            if title:
                entries.append(TocEntry(title, {"section": title}, run, _tokens(run)))
            else:
                entries.append(TocEntry(_first_words(run), {"around": _first_words(run)}, run, _tokens(run)))
        return entries[:_MAX_TOC_ENTRIES]
    if any(loc.get("sheet") for loc in locs):
        return [TocEntry(f"sheet {k!r}", {"sheet": str(k)}, run, _tokens(run))
                for k, run in _runs(body, lambda c: (c.get("locator") or {}).get("sheet"))][:_MAX_TOC_ENTRIES]
    if any(loc.get("slide") for loc in locs):
        return [TocEntry(f"slide {k}", {"slide": str(k)}, run, _tokens(run))
                for k, run in _runs(body, lambda c: (c.get("locator") or {}).get("slide"))][:_MAX_TOC_ENTRIES]
    if any(isinstance(loc.get("page"), int) for loc in locs):
        entries = []
        for group in _sized(body):
            spans = [_span(c.get("locator") or {}, "page", "page_end") for c in group]
            spans = [s for s in spans if s]
            if not spans:
                continue
            lo, hi = min(s[0] for s in spans), max(s[1] for s in spans)
            rng = f"{lo}-{hi}" if hi > lo else str(lo)
            entries.append(TocEntry(f"p. {rng.replace('-', '–')}", {"pages": rng}, group, _tokens(group)))
        return entries[:_MAX_TOC_ENTRIES]
    if any(isinstance(loc.get("line_start"), int) for loc in locs):
        entries = []
        for group in _sized(body):
            spans = [_span(c.get("locator") or {}, "line_start", "line_end") for c in group]
            spans = [s for s in spans if s]
            if not spans:
                continue
            lo, hi = min(s[0] for s in spans), max(s[1] for s in spans)
            entries.append(TocEntry(f"lines {lo}–{hi}", {"lines": f"{lo}-{hi}"}, group, _tokens(group)))
        return entries[:_MAX_TOC_ENTRIES]
    return [TocEntry(_first_words(g), {"around": _first_words(g)}, g, _tokens(g)) for g in _sized(body)][
        :_MAX_TOC_ENTRIES]


def _first_words(chunks: list[dict[str, Any]], n: int = 8) -> str:
    words = " ".join((chunks[0].get("text") or "").split()).split(" ")[:n] if chunks else []
    return " ".join(words)


# ── selection ──────────────────────────────────────────────────────────────


def _select(body: list[dict[str, Any]], toc: list[TocEntry], *, pages, lines, section, sheet, rows, slide,
            around) -> tuple[list[dict[str, Any]], list[str], int | None]:
    """The chunks every given locator selects (they combine with AND), the
    selection's description, and the focus chunk for ``around``."""
    chosen = list(body)
    desc: list[str] = []
    focus: int | None = None
    if pages is not None:
        ranges = parse_ranges(pages, "pages")
        chosen = [c for c in chosen if _overlaps(_span(c.get("locator") or {}, "page", "page_end"), ranges)]
        desc.append(f"pages={pages}")
    if lines is not None:
        ranges = parse_ranges(lines, "lines")
        chosen = [c for c in chosen if _overlaps(_span(c.get("locator") or {}, "line_start", "line_end"), ranges)]
        desc.append(f"lines={lines}")
    if section:
        key = legal_key(section)
        picked: list[dict[str, Any]] = []
        if key and any(c.get("section_key") for c in body):
            picked = [c for c in chosen if (c.get("section_key") or "") == key
                      or (c.get("section_key") or "").startswith(key + "/")]
            if not picked and "/cl:" in key:
                # A short article is one chunk, never split by clause: the
                # clause is inside the article's chunk.
                article = key.split("/", 1)[0]
                picked = [c for c in chosen if (c.get("section_key") or "") == article
                          or (c.get("section_key") or "").startswith(article + "/")]
        if not picked:
            want = fold(section).strip()
            exact = [c for c in chosen if any(fold(h) == want for h in _heading_path(c))]
            picked = exact or [c for c in chosen if any(want in fold(h) for h in _heading_path(c))]
        if not picked:
            labels = [e.arg.get("section") for e in toc if e.arg.get("section")]
            raise ReadError("SectionNotFound", f"No part of this file matches section={section!r}.",
                            _closest(section, [x for x in labels if x]))
        chosen = picked
        desc.append(f"section={section}")
    if sheet:
        want = fold(str(sheet))
        picked = [c for c in chosen if fold(str((c.get("locator") or {}).get("sheet") or "")) == want]
        picked = picked or [c for c in chosen if want in fold(str((c.get("locator") or {}).get("sheet") or ""))]
        if not picked:
            names = sorted({str(loc["sheet"]) for loc in ((c.get("locator") or {}) for c in body) if loc.get("sheet")})
            raise ReadError("SheetNotFound", f"No sheet matches {sheet!r}.", names[:20])
        chosen = picked
        desc.append(f"sheet={sheet}")
    if rows is not None:
        ranges = parse_ranges(rows, "rows")
        chosen = [c for c in chosen if _overlaps(_rows_of(c.get("locator") or {}), ranges)]
        desc.append(f"rows={rows}")
    if slide is not None:
        ranges = parse_ranges(slide, "slide")
        chosen = [c for c in chosen if _overlaps(_span(c.get("locator") or {}, "slide", "slide"), ranges)]
        desc.append(f"slide={slide}")
    if around:
        idx = _find_around(chosen, str(around))
        if idx is None:
            raise ReadError("TextNotFound", f"{around!r} does not occur in this file's indexed text.")
        focus = int(chosen[idx]["id"])
        chosen = chosen[max(0, idx - 1): idx + 2]
        desc.append(f"around={around!r}")
    return chosen, desc, focus


def _find_around(chunks: list[dict[str, Any]], text: str) -> int | None:
    needle = normalize_for_match(text)
    for i, c in enumerate(chunks):
        if needle and needle in normalize_for_match(c.get("text") or ""):
            return i
    folded = fold(text)
    for i, c in enumerate(chunks):
        if folded and folded in fold(c.get("text") or ""):
            return i
    terms = analyze(text)
    best, best_i = 0, None
    for i, c in enumerate(chunks):
        n = matches_text(terms, c.get("text") or "")
        if n > best:
            best, best_i = n, i
    if best_i is not None and best * 2 >= max(1, len(terms.tokens)):
        return best_i
    return None


def _closest(want: str, options: list[str], n: int = 8) -> list[str]:
    import difflib

    folded = {fold(o): o for o in options}
    hits = difflib.get_close_matches(fold(want), list(folded), n=n, cutoff=0.3)
    return [folded[h] for h in hits] or options[:n]


def _rank_toc(toc: list[TocEntry], query: str | None) -> list[TocEntry]:
    if not query:
        return []
    terms = analyze(query)
    if terms.empty:
        return []
    scored = []
    for i, e in enumerate(toc):
        text = e.label + "\n" + "\n".join(c.get("text") or "" for c in e.chunks)
        s = matches_text(terms, text) + sum(2 for p in terms.phrases if fold(p) in fold(text))
        if s:
            scored.append((s, i, e))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [e for _, _, e in scored]


def _is_stale(engine: Any, row: dict[str, Any]) -> bool | None:
    """True when the local file on disk differs from what was indexed (None
    when that cannot be checked: a Drive file, or no folder configured)."""
    if row.get("source") != "local" or not engine.root:
        return None
    path = os.path.join(engine.root, *(row.get("rel_path") or "").split("/"))
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return True
    return int(st.st_size) != int(row.get("size") or -1) or int(st.st_mtime_ns) != int(row.get("mtime_ns") or -1)


def read(
    engine: Any,
    file: str,
    *,
    pages: Any = None,
    lines: Any = None,
    section: str | None = None,
    sheet: str | None = None,
    rows: Any = None,
    slide: Any = None,
    around: str | None = None,
    query: str | None = None,
    page: int = 1,
) -> ReadOutcome:
    row, c8 = resolve_file(engine, file)
    chunks = engine.db.chunks_of_file(int(row["id"]))
    card = next((c for c in chunks if c.get("ctype") == t.CTYPE_FILE_CARD), None)
    body = [c for c in chunks if c.get("ctype") != t.CTYPE_FILE_CARD]
    toc = build_toc(body)
    notes: list[str] = []
    selection: str | None = None
    focus: int | None = None
    selected = body
    asked = any(v not in (None, "") for v in (pages, lines, section, sheet, rows, slide, around))
    if asked:
        selected, desc, focus = _select(body, toc, pages=pages, lines=lines, section=section, sheet=sheet,
                                        rows=rows, slide=slide, around=around)
        selection = ", ".join(desc)
        if not selected:
            notes.append(f"Nothing in this file matches {selection}.")
    elif c8:
        idx = next((i for i, c in enumerate(body) if (c.get("text_hash") or "").startswith(c8)), None)
        if idx is None:
            notes.append(f"The passage [ud:{row['cite_id']}#{c8}] is no longer in this file (it was edited); "
                         "showing the file instead.")
        else:
            focus = int(body[idx]["id"])
            selected = body[max(0, idx - 1): idx + 2]
            selection = f"around [ud:{row['cite_id']}#{c8}]"
    if not body:
        reason = row.get("status_reason") or row.get("status")
        notes.append(f"The content of this file is not indexed ({reason}); only its details are shown.")
    stale = bool(_is_stale(engine, row))
    if stale:
        notes.append("This file changed on disk since it was indexed; it is being re-indexed now, so the "
                     "text below may be outdated.")
        if engine.on_stale is not None:
            engine.on_stale(int(row["id"]))
    return ReadOutcome(
        file=row, card=card, body=body, selected=selected, selection=selection, toc=toc,
        ranked=_rank_toc(toc, query), query=query, stale=stale, notes=notes, focus_id=focus,
        page=max(1, int(page or 1)), tz_name=getattr(engine, "tz_name", ""), tz=getattr(engine, "tz", None),
    )


__all__ = ["ReadError", "ReadOutcome", "TocEntry", "build_toc", "legal_key", "parse_ranges", "read", "resolve_file"]
