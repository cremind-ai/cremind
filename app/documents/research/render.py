"""A research job as text: for the agent (budgeted), REST and the CLI.

What the reader gets depends on where the job is:

- **still working** (``queued`` / ``planning`` / ``running``, and
  ``interrupted``): the phase, the progress and the last steps, then a line
  that tells the agent not to conclude and to call again. Deep research takes
  minutes; an agent that answered from the first poll would answer from
  nothing, so that line is exact and always present.
- **waiting for the user** (``needs_clarification`` / ``needs_confirmation``):
  the question, the candidates to choose from, and the answer keys with the
  exact call that answers them.
- **settled** (``complete`` / ``partial``, and ``failed`` / ``cancelled`` with
  whatever the dossier held): the dossier, in pages. Page 1 is the summary —
  coverage first (how many files were read, which were not and why), the
  authorities and the edition used, the findings per issue with their
  verified quotes, the head of the compiled table and its conflicts, the gaps.
  Later pages carry the long tails (every file of the coverage table, every
  row of the table), read with ``documentation_search__read(file='research:<id>',
  page=n)``.

Budget: like the other renderers (:mod:`app.documents.query.render`), the
result is sized to the profile's tool-result budget by construction — quotes
are shortened first, then whole blocks move to later pages. A block is never
cut through a citation token; a single line too long for a page is cut before
its first incomplete token.

Citations: the tokens are collected from the *final* page text, so exactly
the tokens printed — no more — are issued, each resolved against the index
(the file by its cite id, the passage by its hash prefix). A token whose
passage has since changed is printed but not issued: the saved answer then
reports it as stale rather than verified. Everything derived from documents
(paths, titles, quotes, cell values, the job's own findings, which a model
wrote from document text) goes inside one untrusted-content block per page,
with any ``[doc:`` inside it defanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.documents.cite import TOKEN_RE, IssuedCitation, escape_in_document_text, make_token, parse_tokens
from app.documents.query.render import RenderContext, Rendered, cite_chunk, cite_file, cite_folder
from app.documents.research.types import (
    ACTIVE,
    CANCELLED,
    COMPLETE,
    DOMAIN_LEGAL,
    FAILED,
    INTERRUPTED,
    NEEDS_CLARIFICATION,
    NEEDS_CONFIRMATION,
    PARTIAL,
    READ_FULL,
    READ_NONE,
    READ_PARTIAL,
    ROLE_REFERENCE,
    Authority,
    Cell,
    Clarification,
    CompiledTable,
    CoverageRow,
    Evidence,
    Finding,
    JobView,
)

LEAF = "research"
FN_RESEARCH = "documentation_search__research"
FN_READ = "documentation_search__read"

PRELIMINARY = ("PRELIMINARY — do not conclude from this; call documentation_search__research again with "
               "continue_job={job_id}.")
NOT_LEGAL_ADVICE = ("Not legal advice: this analysis covers only the documents in the user's index; laws, "
                    "editions and facts outside it were not checked.")
CITE_RULE = ("Cite each claim with the [doc:…] token printed next to its quote, copied exactly, and tell the "
             "user which files were not read.")

# Quote lengths tried on page 1 before anything moves to a later page.
_QUOTE_STEPS = (420, 240, 140, 80)
_CELL_CHARS = 160
_TITLE_CHARS = 200
_TEXT_CHARS = 700
# What page 1 lists before pointing at the later pages.
_UNREAD_ON_SUMMARY = 20
_COVERAGE_INLINE = 20
_TABLE_HEAD_ROWS = 10
_CONFLICTS_HEAD = 10
# Unbudgeted pages (REST/CLI): rows per page of the long tails.
_COVERAGE_PAGE_ROWS = 60
_TABLE_PAGE_ROWS = 50
_CONFLICT_PAGE_ROWS = 40
# Table rows per packing block (a block keeps its column header).
_TABLE_BLOCK_ROWS = 10
_STEP_STEPS = (12, 6, 3, 0)
_CANDIDATE_STEPS = (30, 12, 6, 3)
_MAX_COLUMNS = 8

_READ_WORDS = {READ_FULL: "read in full", READ_PARTIAL: "partly read", READ_NONE: "not read"}
_REASONS = {
    "legacy_format": "old file format that cannot be read (e.g. .doc; save it as .docx)",
    "encrypted": "password-protected",
    "awaiting_vision": "image waiting for the vision model to describe it",
    "awaiting_consent": "image waiting for consent to the vision model",
    "over_cap": "image over the daily description quota",
    "too_large": "too large: only its beginning is indexed",
    "error": "could not be extracted",
    "placeholder": "cloud placeholder, not downloaded",
    "metadata_only": "only its name and details are indexed",
    "awaiting_extractor": "waiting for a reader for its format",
    "not_indexed_yet": "not indexed yet",
    "budget": "not read: the token budget ran out",
    "time": "not read: the time limit was reached",
    "not_relevant": "skipped as not relevant",
}


# ── small helpers ──────────────────────────────────────────────────────────


def _clean(text: Any) -> str:
    return escape_in_document_text(str(text or ""))


def _line(text: Any, limit: int) -> str:
    """One line of document-derived text, defanged, at most ``limit`` chars."""
    flat = " ".join(_clean(text).split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + " …"


def _tok(raw: Any) -> str | None:
    """``raw`` as a canonical token, or None when it is not one."""
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.startswith("["):
        text = f"[{text}]"
    found = parse_tokens(text)
    return found[0]["token"] if found else None


def _fid_token(fid: Any) -> str | None:
    fid = str(fid or "").strip().lower()
    return _tok(make_token(fid)) if fid else None


def _n(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _header(view: JobView) -> str:
    used = int(view.tokens_in or 0) + int(view.tokens_out or 0)
    budget = f"{int(view.budget):,}" if view.budget else "?"
    return (f"[Documentation Search · research] job {view.job_id} · {view.status} · {view.mode}/{view.domain} · "
            f"{used:,}/{budget} tokens")


def _question(view: JobView) -> str:
    return f"Question: {_line(view.question, 400)}"


def _page_hint(job_id: str) -> str:
    return f"{FN_READ}(file='research:{job_id}', page=n)"


# ── citations ──────────────────────────────────────────────────────────────


class _Lookup:
    """Resolves printed tokens against the profile's index, once each. A
    failed lookup is a token not issued — never a failed render."""

    def __init__(self, db: Any, leaf: str) -> None:
        self.db = db
        self.leaf = leaf
        self._files: dict[str, dict[str, Any] | None] = {}

    def _file(self, cite_id: str) -> dict[str, Any] | None:
        if cite_id not in self._files:
            row = None
            try:
                row = self.db.file_by_cite(cite_id)
            except Exception:  # noqa: BLE001
                row = None
            if row is not None and row.get("status") in ("missing", "tombstone"):
                row = None
            self._files[cite_id] = row
        return self._files[cite_id]

    def _chunk(self, row: dict[str, Any], c8: str) -> dict[str, Any] | None:
        try:
            found = self.db.read_sql(
                "SELECT * FROM chunks WHERE file_id = ? AND substr(text_hash, 1, 8) = ? ORDER BY ordinal, id LIMIT 1",
                (int(row["id"]), c8), table="chunks")
        except Exception:  # noqa: BLE001
            return None
        return found[0] if found else None

    def citation(self, token: str) -> IssuedCitation | None:
        m = TOKEN_RE.fullmatch(token)
        if m is None:
            return None
        cite_id, c8 = m.group(1), m.group(2)
        row = self._file(cite_id)
        if row is None:
            if c8:
                return None
            try:
                folder = self.db.folder_by_cite(cite_id)
            except Exception:  # noqa: BLE001
                folder = None
            return cite_folder(folder, self.leaf) if folder else None
        if not c8:
            return cite_file(row, self.leaf)
        chunk = self._chunk(row, c8)
        return cite_chunk(row, chunk, self.leaf) if chunk is not None else None


def _citations(text: str, db: Any | None, leaf: str) -> list[IssuedCitation]:
    if db is None:
        return []
    lookup = _Lookup(db, leaf)
    out: list[IssuedCitation] = []
    for token in dict.fromkeys(m.group(0) for m in TOKEN_RE.finditer(text or "")):
        c = lookup.citation(token)
        if c is not None:
            out.append(c)
    return out


# ── blocks and pages ───────────────────────────────────────────────────────


@dataclass
class _Block:
    """Lines that move to another page together. ``page_break`` starts a new
    page in the unbudgeted layout (REST/CLI); the budgeted layout packs."""

    lines: list[str]
    page_break: bool = False


@dataclass
class _Layout:
    summary: list[_Block] = field(default_factory=list)
    detail: list[_Block] = field(default_factory=list)


def _evidence_lines(evidence: Iterable[Evidence], quote_chars: int, indent: str = "  ") -> list[str]:
    out = []
    for ev in evidence:
        token = _tok(ev.token)
        if token is None:
            continue
        where = " · ".join(x for x in (_line(ev.rel_path, 120), _line(ev.label, 80)) if x)
        out.append(f"{indent}\"{_line(ev.quote, quote_chars)}\" {token}" + (f" ({where})" if where else ""))
    return out


def _finding_block(f: Finding, quote_chars: int) -> _Block | None:
    ev = _evidence_lines(f.evidence, quote_chars)
    if not ev:
        # Only verified findings are shown; a pipeline drops the others, but a
        # dossier from an older job may still hold one.
        return None
    head = f"- [{_line(f.stance, 20)}] {_line(f.text, _TEXT_CHARS)}"
    if f.provision:
        head += f" — {_line(f.provision, 160)}"
    return _Block([head, *ev])


def _authority_line(a: Authority) -> str:
    token = _fid_token(a.fid)
    bits = []
    if a.number:
        bits.append(f"No. {_line(a.number, 60)}")
    if a.issued:
        bits.append(f"issued {_line(a.issued, 20)}")
    if a.effective:
        bits.append(f"effective {_line(a.effective, 20)}")
    if a.consolidated:
        bits.append("consolidated text")
    bits.append(_line(a.status, 20).replace("_", " "))
    if a.replaced_by:
        rep = _fid_token(a.replaced_by)
        bits.append(f"replaced by {rep or _line(a.replaced_by, 20)}")
    use = "USED" if a.used else "not used"
    why = f": {_line(a.why, 300)}" if a.why else ""
    title = _line(a.title or a.rel_path, _TITLE_CHARS)
    return f"- {title}{' ' + token if token else ''} — " + " · ".join(bits) + f" — {use}{why}"


def _coverage_line(r: CoverageRow) -> str:
    token = _fid_token(r.fid)
    state = _READ_WORDS.get(r.read, r.read)
    bits = [state]
    if r.reason:
        bits.append(_REASONS.get(r.reason, _line(r.reason, 60)))
    if r.chunks_total:
        bits.append(f"{r.chunks_read}/{r.chunks_total} passages")
    if r.role == ROLE_REFERENCE:
        bits.append("reference")
    return f"- {_line(r.rel_path, 200)}{' ' + token if token else ''} — " + " · ".join(bits)


def _coverage_totals(rows: list[CoverageRow]) -> str:
    full = sum(1 for r in rows if r.read == READ_FULL)
    part = sum(1 for r in rows if r.read == READ_PARTIAL)
    none = sum(1 for r in rows if r.read not in (READ_FULL, READ_PARTIAL))
    refs = sum(1 for r in rows if r.role == ROLE_REFERENCE)
    split = f" ({len(rows) - refs:,} primary, {refs:,} reference)" if refs else ""
    return (f"Coverage: {_n(len(rows), 'file', 'files')} in scope{split} — {full:,} read in full, "
            f"{part:,} partly, {none:,} not read.")


def _cell(c: Cell | None) -> str:
    if c is None:
        return ""
    value = _line(c.value, _CELL_CHARS).replace("|", "\\|")
    tokens = [t for t in (_tok(x) for x in (c.tokens or [])[:2]) if t]
    return " ".join(x for x in (value, *tokens) if x)


def _table_head(columns: list[str]) -> list[str]:
    cols = [_line(c, 60).replace("|", "\\|") for c in columns]
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]


def _table_blocks(table: CompiledTable, rows: list[dict[str, Cell]], title: str | None,
                  *, page_rows: int | None) -> list[_Block]:
    """Rows in blocks of :data:`_TABLE_BLOCK_ROWS`, each with the column
    header; ``page_rows`` (unbudgeted) starts a new page every so many rows."""
    blocks: list[_Block] = []
    for i in range(0, len(rows), _TABLE_BLOCK_ROWS):
        chunk = rows[i:i + _TABLE_BLOCK_ROWS]
        lines = []
        if i == 0 and title:
            lines.append(title)
        lines += _table_head(table.columns)
        lines += ["| " + " | ".join(_cell(r.get(c)) for c in table.columns) + " |" for r in chunk]
        brk = page_rows is not None and i % page_rows == 0
        blocks.append(_Block(lines, page_break=brk))
    return blocks


def _conflict_line(c: Any) -> str:
    values = " vs ".join(_cell(v) for v in c.values) or "(no values)"
    return f"- {_line(c.key, 160)}: {values}"


def _status_line(view: JobView) -> str:
    """How far the dossier can be trusted — outside the untrusted block, as
    it is an instruction to the agent, not document content."""
    status = view.status
    if status == COMPLETE:
        return "Status: complete."
    if status == PARTIAL:
        return ("Status: partial — the job stopped before covering everything; the gaps and notes below say "
                "what is missing.")
    if status == FAILED:
        err = f": {_line(view.error, 400)}" if view.error else ""
        return (f"Status: failed{err}. What the job had found before it failed is below; it is incomplete — do "
                "not present it as a conclusion.")
    if status == CANCELLED:
        return ("Status: cancelled. What the job had found before it was cancelled is below; it is incomplete — "
                "do not present it as a conclusion.")
    return f"Status: {_line(status, 40)}."


def _layout(view: JobView, quote_chars: int, *, paged: bool) -> _Layout:
    """The dossier as blocks. ``paged`` (unbudgeted) marks where the REST/CLI
    pages break; the budgeted layout ignores the marks and packs."""
    d = view.dossier
    out = _Layout()
    if d is None:
        return out
    if d.notes:
        out.summary.append(_Block([f"Note: {_line(n, 400)}" for n in d.notes]))

    # Coverage first: what the answer rests on, and what it does not.
    rows = list(d.coverage)
    if rows:
        cov = [_coverage_totals(rows)]
        if len(rows) <= _COVERAGE_INLINE:
            cov += [_coverage_line(r) for r in rows]
        else:
            unread = [r for r in rows if r.read != READ_FULL]
            if unread:
                cov.append("Not read in full:")
                cov += [_coverage_line(r) for r in unread[:_UNREAD_ON_SUMMARY]]
                if len(unread) > _UNREAD_ON_SUMMARY:
                    cov.append(f"… and {len(unread) - _UNREAD_ON_SUMMARY:,} more on the coverage pages.")
            detail_rows = rows
            for i in range(0, len(detail_rows), _COVERAGE_PAGE_ROWS):
                part = detail_rows[i:i + _COVERAGE_PAGE_ROWS]
                title = [f"Coverage — every file in scope ({i + 1}–{i + len(part)} of {len(rows):,}):"]
                # Budgeted packing moves whole blocks, so keep them small.
                for j in range(0, len(part), 15):
                    lines = (title if j == 0 else []) + [_coverage_line(r) for r in part[j:j + 15]]
                    out.detail.append(_Block(lines, page_break=paged and j == 0))
        out.summary.append(_Block(cov))
    else:
        out.summary.append(_Block(["Coverage: no files in scope."]))

    if d.authorities or d.version_notes:
        lines = ["Authorities and the edition used:"] if d.authorities else []
        lines += [_authority_line(a) for a in d.authorities]
        lines += [f"Version note: {_line(n, 400)}" for n in d.version_notes]
        out.summary.append(_Block(lines))

    facts = [b for b in (_finding_block(f, quote_chars) for f in d.facts) if b]
    if facts:
        out.summary.append(_Block(["Facts (from the primary files):"]))
        out.summary += facts
    for i, issue in enumerate(d.issues, start=1):
        findings = [b for b in (_finding_block(f, quote_chars) for f in issue.findings) if b]
        out.summary.append(_Block([f"Issue {i}: {_line(issue.title, _TITLE_CHARS)}"]
                                  + ([] if findings else ["- (no verified finding)"])))
        out.summary += findings
        tail = []
        if issue.xrefs:
            tail.append("  Provisions followed: " + ", ".join(_line(x, 80) for x in issue.xrefs[:20]))
        tail += [f"  Open question: {_line(q, 300)}" for q in issue.open_questions]
        if tail:
            out.summary.append(_Block(tail))

    # Short and decisive for how far to trust the rest: before the table,
    # so a tight budget never pushes them off the first page.
    if d.gaps:
        out.summary.append(_Block(["Gaps (not covered):"] + [f"- {_line(g, 400)}" for g in d.gaps]))
    if d.rejected_quotes:
        out.summary.append(_Block([
            f"Checked and dropped: {_n(int(d.rejected_quotes), 'quote', 'quotes')} the model gave did not match "
            "the source text."]))

    t = d.compiled
    if t is not None and t.columns:
        n_rows = len(t.rows)
        head_rows = t.rows[:_TABLE_HEAD_ROWS]
        more = n_rows - len(head_rows)
        title = (f"Compiled table — {_n(n_rows, 'row', 'rows')} × {_n(len(t.columns), 'column', 'columns')}"
                 + (f"; the first {len(head_rows)} here, the rest on later pages:" if more > 0 else ":"))
        if head_rows:
            out.summary += _table_blocks(t, head_rows, title, page_rows=None)
        else:
            out.summary.append(_Block([title, "(no rows)"]))
        if more > 0:
            rest = t.rows[_TABLE_HEAD_ROWS:]
            for i in range(0, len(rest), _TABLE_PAGE_ROWS):
                part = rest[i:i + _TABLE_PAGE_ROWS]
                title = (f"Compiled table, rows {_TABLE_HEAD_ROWS + i + 1}–{_TABLE_HEAD_ROWS + i + len(part)} "
                         f"of {n_rows:,}:")
                blocks = _table_blocks(t, part, title, page_rows=None)
                if paged and blocks:
                    blocks[0].page_break = True
                out.detail += blocks
        if t.conflicts:
            shown = t.conflicts[:_CONFLICTS_HEAD]
            lines = ["Conflicts (the sources disagree; every value is kept with its source):"]
            lines += [_conflict_line(c) for c in shown]
            if len(t.conflicts) > len(shown):
                lines.append(f"… and {len(t.conflicts) - len(shown):,} more on later pages.")
                rest = t.conflicts[_CONFLICTS_HEAD:]
                for i in range(0, len(rest), _CONFLICT_PAGE_ROWS):
                    part = rest[i:i + _CONFLICT_PAGE_ROWS]
                    title = [f"Conflicts {_CONFLICTS_HEAD + i + 1}–{_CONFLICTS_HEAD + i + len(part)}:"]
                    for j in range(0, len(part), 10):
                        out.detail.append(_Block((title if j == 0 else []) + [_conflict_line(c) for c in
                                                                                part[j:j + 10]],
                                                 page_break=paged and j == 0))
            out.summary.append(_Block(lines))
    return out


def _artifact_names(view: JobView) -> list[str]:
    d = view.dossier
    if d is None or d.compiled is None or view.status not in (COMPLETE, PARTIAL):
        return []
    return [str(a.get("name") or a.get("uri") or "file") for a in d.compiled.artifacts if isinstance(a, dict)]


def _foot(view: JobView, page: int, pages: int) -> list[str]:
    lines = [CITE_RULE]
    if view.domain == DOMAIN_LEGAL:
        lines.append(NOT_LEGAL_ADVICE)
    if page == 1:
        names = _artifact_names(view)
        if names:
            lines.append("Files attached: " + ", ".join(_line(n, 80) for n in names) + ".")
        if pages == 2:
            lines.append(f"Page 2: {FN_READ}(file='research:{view.job_id}', page=2).")
        elif pages > 2:
            lines.append(f"Pages 2–{pages}: {_page_hint(view.job_id)}.")
    else:
        lines.append(f"Page {page} of {pages}. Other pages: {_page_hint(view.job_id)}, n = 1–{pages}.")
    return lines


def _head(view: JobView, page: int, pages: int) -> list[str]:
    if page == 1:
        head = [_header(view), _question(view), _status_line(view)]
        if view.dossier is None:
            head.append("(The job recorded no dossier.)")
        return head
    return [_header(view), f"Dossier page {page} of {pages} (continued)."]


def _assemble(view: JobView, ctx: RenderContext, page: int, pages: int, body: list[str]) -> str:
    parts = _head(view, page, pages)
    if body:
        parts.append(ctx.wrap("\n".join(body)))
    parts += _foot(view, page, pages)
    return "\n".join(parts)


def _unbudgeted_pages(layout: _Layout) -> list[list[str]]:
    """Page 1 is the summary; the long tails follow, a new page wherever a
    block asks for one."""
    pages: list[list[str]] = [[line for b in layout.summary for line in b.lines]]
    for b in layout.detail:
        if b.page_break or len(pages) == 1:
            pages.append([])
        pages[-1] += b.lines
    return pages


def _cut_line(line: str, room: int, ctx: RenderContext) -> str:
    """``line`` cut to ``room`` tokens, never through a citation token."""
    cut = ctx.cut_tokens(line, max(10, room - 4))
    start = cut.rfind("[doc:")
    if start >= 0 and "]" not in cut[start:]:
        cut = cut[:start]
    return cut.rstrip() + " …(cut)"


def _pack(blocks: list[_Block], rooms: Callable[[int], int], ctx: RenderContext) -> list[list[str]]:
    """Whole blocks onto pages of ``rooms(i)`` tokens; a block bigger than a
    page is split between lines, a line bigger than a page is cut."""
    pages: list[list[str]] = [[]]
    used = 0

    def new_page() -> None:
        nonlocal used
        pages.append([])
        used = 0

    for b in blocks:
        cost = ctx.tokens("\n".join(b.lines)) + 1
        room = rooms(len(pages) - 1)
        if pages[-1] and used + cost > room:
            new_page()
            room = rooms(len(pages) - 1)
        if cost <= room:
            pages[-1] += b.lines
            used += cost
            continue
        for line in b.lines:
            lc = ctx.tokens(line) + 1
            if pages[-1] and used + lc > room:
                new_page()
                room = rooms(len(pages) - 1)
            if lc > room:
                line = _cut_line(line, room, ctx)
                lc = ctx.tokens(line) + 1
            pages[-1].append(line)
            used += lc
    return pages


def _budgeted_pages(view: JobView, ctx: RenderContext) -> list[str]:
    limit = int(ctx.limit or 0)
    wrap_cost = ctx.tokens(ctx.wrap("x")) + 2
    fixed_1 = ctx.tokens("\n".join(_head(view, 1, 99) + _foot(view, 1, 99))) + wrap_cost
    fixed_n = ctx.tokens("\n".join(_head(view, 2, 99) + _foot(view, 2, 99))) + wrap_cost

    # Shrink first: the longest quotes at which the whole summary fits page 1.
    layout = _layout(view, _QUOTE_STEPS[-1], paged=False)
    for q in _QUOTE_STEPS:
        trial = _layout(view, q, paged=False)
        body = [line for b in trial.summary for line in b.lines]
        if ctx.tokens("\n".join(body)) + fixed_1 <= limit:
            layout = trial
            break
    blocks = layout.summary + layout.detail

    margin = 0
    texts: list[str] = []
    for _attempt in range(4):
        def rooms(i: int, _m: int = margin) -> int:
            return max(60, limit - (fixed_1 if i == 0 else fixed_n) - _m)

        pages = _pack(blocks, rooms, ctx)
        texts = [_assemble(view, ctx, i + 1, len(pages), body) for i, body in enumerate(pages)]
        over = max(ctx.tokens(t) - limit for t in texts)
        if over <= 0:
            break
        margin += over + 8
    return texts


def _dossier_texts(view: JobView, ctx: RenderContext) -> list[str]:
    if ctx.limit is None:
        pages = _unbudgeted_pages(_layout(view, _QUOTE_STEPS[0], paged=True))
        return [_assemble(view, ctx, i + 1, len(pages), body) for i, body in enumerate(pages)]
    return _budgeted_pages(view, ctx)


# ── the states that are not a dossier ──────────────────────────────────────


def _fit(ctx: RenderContext, variants: Iterable[Callable[[], str]]) -> str:
    """The first variant that fits (the last one regardless)."""
    text = ""
    for make in variants:
        text = make()
        if ctx.fits(text):
            return text
    return text


def _progress_text(view: JobView, ctx: RenderContext, steps_shown: int) -> str:
    prog = view.progress or {}
    phase = prog.get("phase") or view.phase
    done, total = int(prog.get("done") or 0), int(prog.get("total") or 0)
    lines = [_header(view), _question(view)]
    if view.status == INTERRUPTED:
        where = f" (it was at: {_line(phase, 120)})" if phase else ""
        lines.append(f"Status: interrupted — the server restarted while the job ran{where}. Its checkpoint is "
                     "saved; continuing resumes from there.")
    else:
        bits = [f"Phase: {_line(phase, 120)}" if phase else f"Status: {view.status}"]
        if total:
            bits.append(f"{done:,}/{total:,} done")
        lines.append(" · ".join(bits))
    steps = list(prog.get("steps") or [])[-steps_shown:] if steps_shown else []
    if steps:
        body = [f"- {_line(s.get('label'), 160)} — {_line(s.get('status') or 'running', 20)}" for s in steps]
        lines.append("Latest steps:")
        lines.append(ctx.wrap("\n".join(body)))
    lines.append(PRELIMINARY.format(job_id=view.job_id))
    return "\n".join(lines)


def _cell_value(value: Any, fid_key: bool) -> str:
    if fid_key:
        token = _fid_token(value)
        raw = _line(value, 20)
        return f"{raw} {token}" if token else raw
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _line(", ".join(str(v) for v in value), _CELL_CHARS)
    if isinstance(value, dict):
        return _line(", ".join(f"{k}: {v}" for k, v in value.items()), _CELL_CHARS)
    return _line(value, _CELL_CHARS)


_FID_KEYS = ("fid", "replaced_by")


def _candidate_table(candidates: list[dict[str, Any]], limit: int) -> list[str]:
    rows = [c for c in candidates if isinstance(c, dict)][:limit]
    if not rows:
        return []
    columns: list[str] = []
    for c in rows:
        for k in c:
            if k not in columns:
                columns.append(k)
    # Empty everywhere, or "False" everywhere: not worth a column.
    columns = [k for k in columns if any(c.get(k) not in (None, "", [], {}, False) for c in rows)]
    if "fid" in columns:
        columns.remove("fid")
        columns.insert(0, "fid")
    columns = columns[:_MAX_COLUMNS]
    out = ["| # | " + " | ".join(k.replace("|", "\\|") for k in columns) + " |",
           "|---|" + "---|" * len(columns)]
    for i, c in enumerate(rows, start=1):
        cells = [_cell_value(c.get(k), k in _FID_KEYS).replace("|", "\\|") for k in columns]
        out.append(f"| {i} | " + " | ".join(cells) + " |")
    return out


def _answer_example(keys: dict[str, str]) -> str:
    parts = []
    for k, hint in keys.items():
        h = str(hint or "").lower()
        parts.append(f"\"{k}\": true" if "true" in h and "false" in h else f"\"{k}\": \"…\"")
    return "{" + ", ".join(parts) + "}"


def _question_text(view: JobView, ctx: RenderContext, limit: int) -> str:
    c: Clarification | None = view.clarification
    lines = [_header(view), _question(view),
             f"Status: {view.status} — the job stopped to ask the user; nothing is concluded yet."]
    if c is None:
        lines.append("(The job did not record its question.)")
        lines.append(f"Resume it with {FN_RESEARCH}(continue_job={view.job_id}).")
        return "\n".join(lines)
    table = _candidate_table(list(c.candidates or []), limit)
    body = [f"Question for the user ({_line(c.kind, 20)}): {_line(c.question, 1200)}"]
    if table:
        more = len(c.candidates) - (len(table) - 2)
        body += ["Candidates:", *table]
        if more > 0:
            body.append(f"… and {more} more candidates.")
    lines.append(ctx.wrap("\n".join(body)))
    if c.answer_keys:
        lines.append("Answer keys:")
        lines += [f"- {k}: {_line(v, 200)}" for k, v in c.answer_keys.items()]
    example = _answer_example(c.answer_keys or {"confirm": "true|false"})
    lines.append(f"How to answer: ask the user this question (show the candidates), then call {FN_RESEARCH} with "
                 f"continue_job={view.job_id} and answers={example}. Do not answer the research question "
                 "before the job has finished.")
    return "\n".join(lines)


# ── the public API ─────────────────────────────────────────────────────────


def render_job(view: JobView, *, ctx: RenderContext, db: Any | None, page: int = 1,
               leaf: str = LEAF) -> Rendered:
    """``view`` as text for its reader, sized to ``ctx.limit`` (None: whole
    pages, for REST and the CLI). ``page`` picks a dossier page of a settled
    job. Every token printed is returned as an issued citation, resolved in
    ``db`` (the profile's index; None issues nothing)."""
    status = view.status
    pages = 1
    shown = 1
    files: list[dict[str, Any]] = []
    if status in ACTIVE or status == INTERRUPTED:
        text = _fit(ctx, [lambda n=n: _progress_text(view, ctx, n) for n in _STEP_STEPS])
    elif status in (NEEDS_CLARIFICATION, NEEDS_CONFIRMATION):
        text = _fit(ctx, [lambda n=n: _question_text(view, ctx, n) for n in _CANDIDATE_STEPS])
    else:
        texts = _dossier_texts(view, ctx)
        pages = len(texts)
        shown = min(max(1, int(page or 1)), pages)
        text = texts[shown - 1]
        if int(page or 1) > pages:
            text += f"\n(There is no page {int(page)}; the dossier has {_n(pages, 'page', 'pages')}.)"
        if status in (COMPLETE, PARTIAL) and view.dossier is not None and view.dossier.compiled is not None:
            files = [dict(a) for a in view.dossier.compiled.artifacts if isinstance(a, dict)]
    citations = _citations(text, db, leaf)
    data = {"leaf": leaf, "job_id": view.job_id, "status": status, "mode": view.mode, "domain": view.domain,
            "page": shown, "pages": pages}
    return Rendered(text=text, citations=citations, files=files, data=data)


def _plain_context() -> RenderContext:
    from app.tools.builtin.external_content import wrap_document_content

    def fit_lines(text: str, cap: int, **_kw: Any) -> tuple[str, bool]:
        return text, False

    return RenderContext(limit=None, tokens=lambda s: len(s or "") // 4, fit_lines=fit_lines,
                         cut_tokens=lambda s, n: s, wrap=wrap_document_content)


def dossier_pages(view: JobView) -> list[str]:
    """Every page of ``view``, unbudgeted (REST ``pages``, the CLI's
    ``--all-pages``). A job that is not settled is one page: its status."""
    ctx = _plain_context()
    if view.status in ACTIVE or view.status in (INTERRUPTED, NEEDS_CLARIFICATION, NEEDS_CONFIRMATION):
        return [render_job(view, ctx=ctx, db=None).text]
    return _dossier_texts(view, ctx)


__all__ = ["FN_READ", "FN_RESEARCH", "LEAF", "NOT_LEGAL_ADVICE", "PRELIMINARY", "dossier_pages", "render_job"]
