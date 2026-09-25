"""Structure-aware, content-defined chunking: the heart of "only re-embed what
changed".

Why content-defined. A file edit re-extracts the whole file (unavoidable for
binary formats), re-chunks it, and the diff keeps every chunk whose
``text_hash`` already exists. That only saves work if an edit in one place
leaves the chunk *boundaries* elsewhere where they were. Fixed-size or
greedy-fill chunking fails that: insert one paragraph near the top and every
later boundary shifts, every later hash changes, and the whole file is
re-embedded. Here whether a cut falls between two blocks depends only on the
text within a window around that spot, so an edit can only move cuts near
itself and the rest of the file hashes exactly as before (a property test
holds this to ≤3 changed chunks for ≥95% of random single-paragraph edits;
measured ~99%).

The algorithm, over blocks in reading order (sizes are
:func:`~app.documents.textnorm.estimate_tokens` estimates, MIN=120,
TARGET=300, MAX=420, WINDOW=0.6·TARGET=180):

1. Legal documents (≥5 article headings, or ``legal=True``) first go through
   :func:`~app.documents.chunking.legal.apply_legal_structure`.
2. A block over MAX is split into sentences (``;`` also ends one in legal
   text); a sentence still over MAX is split with a Gear rolling hash over
   its characters (32-char window, a cut where the hash's top bits are zero,
   the mask sized for ~TARGET tokens, a hard stop before MAX). The pieces
   become units of their own and follow the same rules as blocks.
3. HARD anchors (H1–H3, article, slide, sheet, email part) always cut: they
   split the unit stream into sections, and no chunk crosses one.
4. A section that fits in MAX is one chunk (a short article stays whole).
5. In a longer section every gap between two units that does not follow a
   heading is a candidate cut, ranked by (SOFT anchor next? first) then
   ``blake2b(normalize_ws(unit.text), digest_size=8)`` of the unit it would
   end. A candidate is an *anchor* when its rank is strictly the best of all
   candidates within WINDOW tokens on either side and it leaves ≥ MIN on both
   sides of the section. The section is cut at every anchor.
6. A stretch between anchors still over MAX is split at its best-ranked
   candidate that leaves ≥ MIN on both sides, until every piece fits.

Why anchors, not the greedy "flush when ≥ MIN and hash % k == 0, or before
MAX" rule the design first specified: the greedy rule's size-driven cuts
(the MAX flush, and the switch from k=8 to k=2 at TARGET) depend on where
the current chunk started, so after an edit the old and new boundary
sequences stay out of step until they happen to cut at the same block. On
synthetic 200-chunk documents it kept only 90-96% of edits at ≤3 changed
chunks, with tails of 6-9; the anchor rule keeps 99-100% on the same
documents. An anchor's status depends only on the ±WINDOW around it, and step 6
only ever looks between two anchors, so no decision can drift further than
that. SOFT anchors keep the design's intent: a clause, sub-heading or page
break wins any window it is in, and two cuts are always more than WINDOW
(= the design's 0.6·TARGET SOFT threshold) apart.

Headings are never cut after (a heading belongs to the chunk it starts). The
one refinement: when a section holds nothing but headings strictly shallower
than an incoming HARD heading in the same slide/sheet/part ("# Title" then
"## Intro", "Chương II" then "Điều 12"), the HARD cut is skipped and the
parent heading rides along, instead of being embedded as a chunk that says
nothing. Two headings of the same depth (two articles, two slides) still
never share a chunk.

Each chunk carries a breadcrumb (the heading path all its units share, not
counting parent headings that rode along; capped at 48 estimated tokens by
dropping ancestors from the left) that is
part of the embedded text and therefore of ``text_hash``. File names and
paths are deliberately *not* in body chunks — they live in the file card —
so a rename re-embeds one card, not the file.

``CHUNKER_VERSION``: bump it whenever the output for identical input blocks
changes (boundaries, text, breadcrumb, hash, locator, refs, folded text).
Files record the version they were chunked with and are re-chunked on
mismatch; the diff then re-embeds only the chunks whose hashes moved, so a
bump that leaves hashes alone costs DB updates, not embeddings.
"""

from __future__ import annotations

import functools
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.documents import textnorm
from app.documents.chunking import legal as _legal
from app.documents.types import ANCHOR_HARD, ANCHOR_SOFT, CTYPE_BODY, Block, Chunk

CHUNKER_VERSION = 1

MIN_TOKENS = 120
TARGET_TOKENS = 300
MAX_TOKENS = 420
# Breadcrumb budget, including the newline that joins it to the text. With
# e5's "passage: " prefix (~3) a chunk's embedded input stays ≤ 471
# estimated tokens — and the estimate over-counts — inside the 512 window.
HEADING_MAX_TOKENS = 48
SOFT_CUT_FILL = 0.6
# How far (in estimated tokens) a cut candidate must out-rank its neighbours
# to become an anchor — and so the least distance between two anchors.
ANCHOR_WINDOW = int(SOFT_CUT_FILL * TARGET_TOKENS)
HEADING_SEP = " › "
_ELLIPSIS = "…"
# Longest heading label kept before the token cap applies (bounds the cost
# of capping a pathological one-line "heading").
_LABEL_MAX_CHARS = 200

# Re-chunking an edited file recomputes the same per-block and per-chunk
# values for everything the edit did not touch — and a file being worked on
# is re-chunked on every save. These pure functions of a string are memoised
# (thread-safe lru_cache) so a re-chunk costs roughly what the edit changed.
# Only strings up to _CACHE_MAX_CHARS are cached, which bounds the memory the
# caches can pin (a few MB typical, tens of MB worst case) no matter what
# files come through.
_CACHE_ENTRIES = 4096
_CACHE_MAX_CHARS = 4096

_tokens_cached = functools.lru_cache(maxsize=_CACHE_ENTRIES)(textnorm.estimate_tokens)


def _tokens(text: str) -> int:
    if len(text) <= _CACHE_MAX_CHARS:
        return _tokens_cached(text)
    return textnorm.estimate_tokens(text)


# (text_hash, refs frozen as tuples of items, token_est, folded)
_Derived = tuple[str, tuple[tuple[tuple[str, Any], ...], ...], int, str | None]


def _derived_uncached(heading: str, text: str) -> _Derived:
    """(text_hash, refs, token_est, folded) of one chunk. Refs are frozen
    so a cached value can be shared; each chunk gets its own dict copies."""
    embed = f"{heading}\n{text}" if heading else text
    refs = tuple(tuple(r.items()) for r in _legal.extract_refs(text))
    return (textnorm.text_hash(heading, text), refs, textnorm.estimate_tokens(embed),
            textnorm.fold_if_needed(embed))


_derived_cached = functools.lru_cache(maxsize=_CACHE_ENTRIES // 4)(_derived_uncached)


def _derived(heading: str, text: str) -> _Derived:
    if len(heading) + len(text) <= 2 * _CACHE_MAX_CHARS:
        return _derived_cached(heading, text)
    return _derived_uncached(heading, text)


@dataclass(slots=True)
class _Unit:
    """A block, or one piece of a block that was too big to be a unit."""

    block: int            # index into the (overlaid) block list
    start: int            # char span in that block's cleaned text
    end: int
    text: str             # the span, trimmed — what is hashed and counted
    tokens: int
    first: bool           # the block's first piece: its anchor applies here
    anchor: int
    heading: bool
    level: int
    locator: dict[str, Any]
    path: list[str] = field(default_factory=list)


# ── Text cleanup ───────────────────────────────────────────────────────────

_TRAILING_WS_RE = re.compile(r"[ \t\f\v\u00a0]+(?=\n|$)")


def _clean(text: str) -> str:
    """NFC, LF line endings, no trailing blanks, no leading/trailing blank
    lines. Leading indentation is kept (code)."""
    t = unicodedata.normalize("NFC", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = _TRAILING_WS_RE.sub("", t)
    return t.strip("\n").rstrip()


def _label(text: str) -> str:
    """A heading block's breadcrumb label: its first line, Markdown '#'s off."""
    for line in text.split("\n"):
        s = line.strip().lstrip("#").strip()
        if s:
            return " ".join(s.split())[:_LABEL_MAX_CHARS]
    return ""


# ── Splitting an oversized block ───────────────────────────────────────────

_SENT_CLOSERS = "\"”’»)\\]"
_SENT_RE = re.compile(
    rf"[.!?…]+[{_SENT_CLOSERS}]*\s+|[。！？]+[{_SENT_CLOSERS}」』]*\s*|\n+"
)
_SENT_RE_LEGAL = re.compile(
    rf"[.!?…;]+[{_SENT_CLOSERS}]*\s+|[。！？；]+[{_SENT_CLOSERS}」』]*\s*|\n+"
)


def _sentence_spans(text: str, legal: bool) -> list[tuple[int, int]]:
    """Contiguous spans covering ``text``, each ending after a sentence end
    (and the whitespace that follows it) or a line break."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in (_SENT_RE_LEGAL if legal else _SENT_RE).finditer(text):
        cut = m.end()
        if cut <= pos:
            continue
        if text[pos:cut].strip():
            spans.append((pos, cut))
            pos = cut
        elif spans:
            # A run of pure whitespace joins the previous span.
            spans[-1] = (spans[-1][0], cut)
            pos = cut
    if pos < len(text):
        if text[pos:].strip() or not spans:
            spans.append((pos, len(text)))
        else:
            spans[-1] = (spans[-1][0], len(text))
    return spans


# Gear table: 256 fixed pseudo-random 32-bit values. Derived from blake2b of
# the index so it is identical on every machine and every run — boundaries
# must never depend on process state.
_GEAR = [
    int.from_bytes(hashlib.blake2b(bytes([i]), digest_size=4, person=b"cremind-gear").digest(), "big")
    for i in range(256)
]
_MASK32 = 0xFFFFFFFF
# Stop a Gear piece this far below MAX so the rounded estimate of the piece
# can never exceed MAX (one character adds at most ~2.2).
_GEAR_HARD_MARGIN = 4


def _is_word_char(ch: str) -> bool:
    return textnorm._WORD_RE.match(ch) is not None


def _char_costs(text: str) -> list[float]:
    """Per-character share of :func:`textnorm.estimate_tokens`, using the same
    character classes: summed over any piece it is never less than the
    piece's raw estimate (it errs high where the two differ, e.g. upper case
    outside Latin/Greek/Cyrillic), so a running sum can decide where a piece
    must end."""
    dense = textnorm._DENSE_RE
    punct = textnorm._PUNCT_RE
    costs: list[float] = []
    prev_word = False
    word_len = 0
    digit_run = 0
    nl_in_ws = False
    for ch in text:
        c = 0.0
        if dense.match(ch):
            c = textnorm.DENSE_COST
            prev_word, word_len, digit_run, nl_in_ws = False, 0, 0, False
        elif _is_word_char(ch):
            if not prev_word:
                c += textnorm.WORD_COST
                word_len = 0
            elif ch.isupper():
                c += textnorm.INNER_UPPER_COST
            word_len += 1
            if word_len > textnorm.LONG_WORD_CHARS:
                c += 1.0 / textnorm.LONG_WORD_CHARS_PER_TOKEN
            if ch.isdigit():
                digit_run += 1
                if digit_run > 2:
                    c += textnorm.DIGIT_COST
            else:
                digit_run = 0
            prev_word, nl_in_ws = True, False
        elif ch.isspace():
            if ch == "\n" and not nl_in_ws:
                c = textnorm.NEWLINE_RUN_COST
                nl_in_ws = True
            prev_word, word_len, digit_run = False, 0, 0
        else:
            if punct.match(ch):
                c = textnorm.PUNCT_COST
            prev_word, word_len, digit_run, nl_in_ws = False, 0, 0, False
        costs.append(c)
    return costs


def _gear_spans(text: str) -> list[tuple[int, int]]:
    """Content-defined character spans for a sentence too long to keep whole.

    Cut candidates are word starts when the text has spaces (so words stay
    whole), otherwise every character (CJK, base64, minified code). A cut is
    taken where the top ``bits`` of the 32-bit Gear hash are zero — the top
    bits depend on the last 32 characters, the low ones on far fewer — once
    the piece has MIN tokens; failing that, at the last candidate before the
    piece would pass MAX.
    """
    n = len(text)
    costs = _char_costs(text)
    spaced = sum(1 for ch in text if ch.isspace()) * 40 >= n

    def eligible(i: int) -> bool:
        return not spaced or (text[i - 1].isspace() and not text[i].isspace())

    candidates = sum(1 for i in range(1, n) if eligible(i))
    per_token = max(candidates, 1) / max(1.0, sum(costs))
    expected = max(2.0, (TARGET_TOKENS - MIN_TOKENS) * per_token)
    bits = max(1, min(24, round(math.log2(expected))))
    top_mask = ((1 << bits) - 1) << (32 - bits)
    hard = MAX_TOKENS - _GEAR_HARD_MARGIN

    def piece_cost(a: int, b: int) -> float:
        # A piece that starts inside a word prices that word as a new one,
        # as estimate_tokens will; the global costs[] did not.
        extra = textnorm.WORD_COST if (
            0 < a < n and _is_word_char(text[a]) and _is_word_char(text[a - 1])
        ) else 0.0
        return extra + sum(costs[a:b])

    spans: list[tuple[int, int]] = []
    start = 0
    run = 0.0          # cost of text[start:i]
    last_ok = -1       # last candidate past MIN in the current piece
    h = 0
    for i in range(n):
        if i > start:
            if eligible(i) and run >= MIN_TOKENS:
                if (h & top_mask) == 0:
                    spans.append((start, i))
                    start, run, last_ok = i, piece_cost(i, i), -1
                else:
                    last_ok = i
            # (Re-checked: a hash cut may just have moved ``start`` to i.)
            if i > start and run + costs[i] + textnorm.WORD_COST > hard:
                cut = last_ok if last_ok > start else i
                spans.append((start, cut))
                start, run, last_ok = cut, piece_cost(cut, i), -1
        run += costs[i]
        h = ((h << 1) + _GEAR[ord(text[i]) & 0xFF]) & _MASK32
    if start < n:
        spans.append((start, n))
    return spans


# ── Units ──────────────────────────────────────────────────────────────────


def _block_units(idx: int, b: Block, text: str, legal: bool) -> list[_Unit]:
    heading = b.role == "heading"
    tokens = _tokens(text)
    if tokens <= MAX_TOKENS:
        return [_Unit(idx, 0, len(text), text, tokens, True, b.anchor, heading, b.level,
                      b.locator)]
    spans: list[tuple[int, int]] = []
    for s, e in _sentence_spans(text, legal):
        piece = text[s:e]
        if textnorm.estimate_tokens(piece) <= MAX_TOKENS:
            spans.append((s, e))
        else:
            spans.extend((s + a, s + z) for a, z in _gear_spans(piece))
    units: list[_Unit] = []
    base_line = b.locator.get("line_start")
    for s, e in spans:
        piece = text[s:e].strip()
        if not piece:
            continue
        loc = b.locator
        if isinstance(base_line, int):
            # Narrow the line span to the piece, for precise citations.
            lead = len(text[s:e]) - len(text[s:e].lstrip())
            first_line = base_line + text.count("\n", 0, s + lead)
            loc = dict(b.locator)
            loc["line_start"] = first_line
            loc["line_end"] = first_line + piece.count("\n")
        units.append(_Unit(idx, s, e, piece, _tokens(piece), not units,
                           b.anchor if not units else 0, heading, b.level, loc))
    return units


def _heading_paths(blocks: list[Block]) -> list[list[str]]:
    """The heading path in effect at each block: ``locator['heading']`` when
    the extractor (or the legal overlay) supplied one, else a stack kept from
    heading blocks by level (a heading of unknown level nests below all)."""
    stack: list[tuple[int, str]] = []
    out: list[list[str]] = []
    for b in blocks:
        given = b.locator.get("heading")
        if isinstance(given, list):
            out.append([str(x) for x in given if str(x).strip()])
            continue
        if b.role == "heading":
            level = b.level if 1 <= b.level <= 6 else 7
            label = _label(b.text)
            if label:
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, label))
        out.append([label for _, label in stack])
    return out


def _cut_hash_uncached(text: str) -> int:
    digest = hashlib.blake2b(textnorm.normalize_ws(text).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


_cut_hash_cached = functools.lru_cache(maxsize=_CACHE_ENTRIES)(_cut_hash_uncached)


def _cut_hash(text: str) -> int:
    """blake2b-64 of the normalized unit text: its rank as a cut point."""
    if len(text) <= _CACHE_MAX_CHARS:
        return _cut_hash_cached(text)
    return _cut_hash_uncached(text)


def _carries(cur: list[_Unit], u: _Unit) -> bool:
    """May ``cur`` (all headings) stay in the chunk the HARD heading ``u``
    starts? Only when every heading in it is strictly shallower and in the
    same slide/sheet/part — a parent riding along with its first child."""
    if not u.heading or u.level <= 0:
        return False
    for c in cur:
        if not c.heading or not (0 < c.level < u.level):
            return False
        for key in ("slide", "sheet", "part"):
            if c.locator.get(key) != u.locator.get(key):
                return False
    return True


def _separator_cost(prev: _Unit, u: _Unit) -> int:
    # Between blocks the join is "\n\n": one newline run. Between pieces of
    # one block the text is contiguous, but a Gear cut can split a word, and
    # re-joining it may cost up to ~1.9 more than the two halves did.
    return 1 if prev.block != u.block else 2


def _cumulative(units: list[_Unit]) -> list[int]:
    """``cum[p]`` = size of ``units[:p + 1]`` including separators — an upper
    bound on the estimate of the text those units join into."""
    cum: list[int] = []
    size = 0
    for i, u in enumerate(units):
        if i:
            size += _separator_cost(units[i - 1], u)
        size += u.tokens
        cum.append(size)
    return cum


def _sections(units: list[_Unit]) -> list[list[_Unit]]:
    """Split at HARD anchors (a chunk never spans one), letting a run of
    parent headings ride along with their first child heading."""
    sections: list[list[_Unit]] = []
    cur: list[_Unit] = []
    for u in units:
        if cur and u.first and u.anchor == ANCHOR_HARD and not _carries(cur, u):
            sections.append(cur)
            cur = []
        cur.append(u)
    if cur:
        sections.append(cur)
    return sections


def _cut_key(units: list[_Unit], p: int) -> tuple[int, int]:
    """Rank of the cut between ``units[p]`` and ``units[p + 1]`` — lower is
    stronger. A cut in front of a SOFT anchor (clause, H4–H6, page break)
    outranks any other; ties among equals go to the content hash of the unit
    the cut would end. Both depend only on the text at the cut."""
    nxt = units[p + 1]
    soft = nxt.first and nxt.anchor == ANCHOR_SOFT
    return (0 if soft else 1, _cut_hash(units[p].text))


def _anchor_cuts(units: list[_Unit], cum: list[int]) -> list[int]:
    """Content-defined cut positions in a section: ``p`` whose rank is
    strictly the best among all candidate cuts within ANCHOR_WINDOW tokens on
    either side, and that leaves at least MIN on both sides of the section.

    Whether ``p`` is an anchor depends only on the units within the window
    around it, so an edit moves anchors only within a window of itself —
    the property the greedy rule lacked. Two anchors are always more than
    ANCHOR_WINDOW apart, which also bounds how small a chunk can get.
    """
    total = cum[-1]
    # Never cut right after a heading: it belongs to what follows.
    cands = [p for p in range(len(units) - 1) if not units[p].heading]
    if not cands:
        return []
    keys = [_cut_key(units, p) for p in cands]
    xs = [cum[p] for p in cands]
    n = len(cands)
    ok = [True] * n
    # Sliding-window minimum, once looking left and once looking right. A
    # monotonic deque keeps it linear however small the units are (CSV rows,
    # lines of code).
    for order in (range(n), range(n - 1, -1, -1)):
        dq: list[int] = []
        head = 0
        for i in order:
            while head < len(dq) and abs(xs[dq[head]] - xs[i]) > ANCHOR_WINDOW:
                head += 1
            if head < len(dq) and not keys[i] < keys[dq[head]]:
                ok[i] = False
            while len(dq) > head and keys[dq[-1]] >= keys[i]:
                dq.pop()
            dq.append(i)
    cuts = []
    for i, p in enumerate(cands):
        if ok[i] and cum[p] >= MIN_TOKENS:
            right = total - cum[p] - _separator_cost(units[p], units[p + 1])
            if right >= MIN_TOKENS:
                cuts.append(p)
    return cuts


def _split_oversize(units: list[_Unit]) -> list[list[_Unit]]:
    """Split a run of units that exceeds MAX at its best-ranked cut, preferring
    cuts that leave MIN on both sides, until every piece fits. Everything it
    looks at lies between two content-defined anchors, so its choices cannot
    spread past them.

    Equal ranks only arise from identical text (a log or table of repeated
    lines, where no anchor can win either); those ties go to the cut nearest
    the middle so the splitting stays balanced — iterative, and O(n log n)
    rather than peeling one MIN-sized chunk at a time off a huge run.
    """
    out: list[list[_Unit]] = []
    todo = [units]
    while todo:
        run = todo.pop()
        cum = _cumulative(run)
        total = cum[-1]
        if total <= MAX_TOKENS or len(run) == 1:
            out.append(run)
            continue
        best: tuple[tuple[int, int, int, float], int] | None = None
        for p in range(len(run) - 1):
            left = cum[p]
            right = total - left - _separator_cost(run[p], run[p + 1])
            balanced = 0 if (left >= MIN_TOKENS and right >= MIN_TOKENS) else 1
            # A cut after a heading only as a last resort.
            rank = balanced + (2 if run[p].heading else 0)
            key = (rank, *_cut_key(run, p), abs(2 * left - total))
            if best is None or key < best[0]:
                best = (key, p)
        assert best is not None
        p = best[1]
        todo.append(run[p + 1:])  # LIFO: the left part is emitted first
        todo.append(run[: p + 1])
    return out


def _group(units: list[_Unit]) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    for sec in _sections(units):
        cum = _cumulative(sec)
        if cum[-1] <= MAX_TOKENS:
            groups.append(sec)  # a section that fits stays whole
            continue
        start = 0
        for p in _anchor_cuts(sec, cum) + [len(sec) - 1]:
            groups.extend(_split_oversize(sec[start: p + 1]))
            start = p + 1
    return groups


# ── Chunk assembly ─────────────────────────────────────────────────────────


def _common_prefix(paths: list[list[str]]) -> list[str]:
    if not paths:
        return []
    first = paths[0]
    n = len(first)
    for p in paths[1:]:
        n = min(n, len(p))
        for i in range(n):
            if p[i] != first[i]:
                n = i
                break
    return list(first[:n])


def _cap_breadcrumb(path: list[str]) -> str:
    """Join ``path`` with ' › ' and fit it in HEADING_MAX_TOKENS (counting the
    newline that follows it), dropping ancestors from the left first, then
    cutting the last label from the left. Truncation is marked with '…'."""
    if not path:
        return ""

    def fits(s: str) -> bool:
        return textnorm.estimate_tokens(s) + 1 <= HEADING_MAX_TOKENS

    s = HEADING_SEP.join(path)
    if fits(s):
        return s
    for i in range(1, len(path)):
        s = _ELLIPSIS + HEADING_SEP + HEADING_SEP.join(path[i:])
        if fits(s):
            return s
    last = path[-1]
    lo, hi = 0, len(last)
    while lo < hi:  # smallest left cut that fits
        mid = (lo + hi) // 2
        if fits(_ELLIPSIS + last[mid:].lstrip()):
            hi = mid
        else:
            lo = mid + 1
    return _ELLIPSIS + last[lo:].lstrip()


_A1_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def _col_num(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _col_name(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _parse_range(rng: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(rng, str):
        return None
    parts = rng.split(":")
    cells = []
    for p in parts[:2]:
        m = _A1_RE.match(p.strip())
        if not m:
            return None
        cells.append((_col_num(m.group(1)), int(m.group(2))))
    if len(cells) == 1:
        cells.append(cells[0])
    (c1, r1), (c2, r2) = cells
    return min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)


def _core(units: list[_Unit]) -> list[_Unit]:
    """The units a chunk is *about*: all but the parent headings that rode
    along at its start ("Chương II" in front of "Điều 7"). Of a leading run
    of headings only the last — the chunk's own heading — is kept."""
    lead = 0
    while lead < len(units) and units[lead].heading:
        lead += 1
    # Pieces of one oversized heading block are one heading, not a run.
    first_of_last = lead - 1
    while first_of_last > 0 and units[first_of_last - 1].block == units[lead - 1].block:
        first_of_last -= 1
    return units[max(0, first_of_last):]


def _merge_locators(units: list[_Unit], core: list[_Unit], breadcrumb: list[str]) -> dict[str, Any]:
    """One locator for a chunk: the span its units cover."""
    locs = [u.locator for u in units]
    out: dict[str, Any] = {}
    handled = {"page", "page_end", "line_start", "line_end", "sheet", "range", "rows",
               "para", "slide", "part", "heading", "article", "clause", "point"}
    for loc in locs:  # unknown keys: first value wins
        for k, v in loc.items():
            if k not in handled and k not in out:
                out[k] = v

    pages = [lc["page"] for lc in locs if isinstance(lc.get("page"), int)]
    if pages:
        out["page"] = min(pages)
        last = max(max(lc.get("page_end") or lc["page"], lc["page"])
                   for lc in locs if isinstance(lc.get("page"), int))
        if last > out["page"]:
            out["page_end"] = last
    lines = [lc["line_start"] for lc in locs if isinstance(lc.get("line_start"), int)]
    if lines:
        out["line_start"] = min(lines)
        out["line_end"] = max(lc.get("line_end") or lc["line_start"]
                              for lc in locs if isinstance(lc.get("line_start"), int))
    sheets = [lc for lc in locs if lc.get("sheet") is not None]
    if sheets:
        sheet = sheets[0]["sheet"]
        out["sheet"] = sheet
        boxes = [_parse_range(lc.get("range")) for lc in sheets if lc["sheet"] == sheet]
        boxes = [b for b in boxes if b]
        if boxes:
            c1 = min(b[0] for b in boxes)
            r1 = min(b[1] for b in boxes)
            c2 = max(b[2] for b in boxes)
            r2 = max(b[3] for b in boxes)
            out["range"] = f"{_col_name(c1)}{r1}:{_col_name(c2)}{r2}"
        elif sheets[0].get("range") is not None:
            out["range"] = sheets[0]["range"]
    for key in ("rows", "para"):
        spans = [lc[key] for lc in locs
                 if isinstance(lc.get(key), (list, tuple)) and len(lc[key]) == 2]
        if spans:
            out[key] = [min(s[0] for s in spans), max(s[1] for s in spans)]
    for key in ("slide", "part"):
        for lc in locs:
            if lc.get(key) is not None:
                out[key] = lc[key]
                break

    # Legal fields: the provision the chunk lies wholly within. Headings that
    # rode along (a parent Chương) do not count against that; the chunk's own
    # article heading does, so a whole article is "art:12", not its clause 1.
    content = [u.locator for u in core]
    for key in ("article", "clause", "point"):
        vals = {lc.get(key) for lc in content}
        if len(vals) == 1 and None not in vals:
            out[key] = vals.pop()
        elif key == "article":
            break  # no single article → no clause/point either
    if "clause" not in out:
        out.pop("point", None)
    if breadcrumb:
        out["heading"] = list(breadcrumb)
    return out


def _make_chunk(ordinal: int, units: list[_Unit], texts: list[str], ctype: str) -> Chunk:
    # Text: consecutive pieces of one block are a contiguous slice of it;
    # different blocks are separated by a blank line.
    parts: list[str] = []
    i = 0
    while i < len(units):
        j = i
        while j + 1 < len(units) and units[j + 1].block == units[i].block:
            j += 1
        part = texts[units[i].block][units[i].start:units[j].end].rstrip().lstrip("\n")
        if part.strip():
            parts.append(part)
        i = j + 1
    text = "\n\n".join(parts)

    # Breadcrumb: the heading path every core unit shares — the smallest
    # section holding the whole chunk.
    core = _core(units)
    path = _common_prefix([u.path for u in core])
    heading = _cap_breadcrumb(path)
    locator = _merge_locators(units, core, path)
    thash, refs, token_est, folded = _derived(heading, text)
    return Chunk(
        ordinal=ordinal,
        ctype=ctype,
        heading=heading,
        text=text,
        text_hash=thash,
        occ=0,
        section_key=_legal.section_key_for(locator),
        locator=locator,
        refs=[dict(r) for r in refs],
        token_est=token_est,
        folded=folded,
    )


def _chunk(blocks: list[Block], legal: bool | None, ctype: str) -> list[Chunk]:
    if legal is None:
        legal = _legal.looks_legal(blocks)
    if legal:
        blocks = _legal.apply_legal_structure(blocks)

    texts = [_clean(b.text) for b in blocks]
    paths = _heading_paths(blocks)
    units: list[_Unit] = []
    for idx, (b, text) in enumerate(zip(blocks, texts)):
        if not text.strip():
            continue
        for u in _block_units(idx, b, text, bool(legal)):
            u.path = paths[idx]
            units.append(u)

    chunks = [_make_chunk(i, g, texts, ctype) for i, g in enumerate(_group(units))]
    seen: dict[str, int] = {}
    for c in chunks:
        c.occ = seen.get(c.text_hash, 0)
        seen[c.text_hash] = c.occ + 1
    return chunks


def chunk_blocks(blocks: list[Block], *, legal: bool | None = None) -> list[Chunk]:
    """Chunk a file's extracted blocks into ``body`` chunks (see the module
    docstring for the rules). ``legal=None`` detects legal structure itself.

    Deterministic: the same blocks always give identical chunks. ``occ``
    counts duplicates within this call only — :func:`diff_chunks` renumbers
    it over the file's full chunk list.
    """
    return _chunk(list(blocks), legal, CTYPE_BODY)


__all__ = [
    "CHUNKER_VERSION",
    "HEADING_MAX_TOKENS",
    "MAX_TOKENS",
    "MIN_TOKENS",
    "TARGET_TOKENS",
    "chunk_blocks",
]
