"""Quote verification: does a quoted passage really occur in the cited source?

An answer that quotes a document is only as trustworthy as the quote. Models
paraphrase inside quotation marks, "fix" typography, drop a diacritic, or
round a number — and a legal or financial answer built on such a quote is
wrong in exactly the place a reader would not think to check. So every quote
placed before a citation is checked against the chunk the citation names (and
that chunk's neighbours, since a quote may straddle a chunk boundary):

- **exact** — the quote is a substring of the source;
- **normalized** — equal after :func:`~app.userdocs.textnorm.normalize_for_match`
  (quotes, dashes, ellipses, case, whitespace — typography, never letters);
- **fuzzy** — a window of the source, anchored on the quote's first or last
  three words, is close enough (difflib ratio ≥ 0.92 for 40+ characters,
  ≥ 0.97 for 12–39) **and** has the identical multiset of digits **and** does
  not differ only by diacritics. The last two are the point: "Điều 203" is not
  "Điều 208", and in Vietnamese "đất" (land) is not "đát". A diacritics-only
  difference is tolerated only when the source has no diacritics at all — an
  unaccented document quoted with its accents restored;
- **mismatch** — none of the above.

A quote may elide with "…": the pieces are checked in order, in the same
source. A leading or trailing ellipsis just means "the quote starts/ends
mid-sentence".

The result also carries the source's own wording for the quote
(``canonical``), which research (PR6) uses in place of the model's.
"""

from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from app.userdocs.textnorm import normalize_for_match

EXACT = "exact"
NORMALIZED = "normalized"
FUZZY = "fuzzy"
MISMATCH = "mismatch"

# Best first. Also the order "worst of several quotes" is taken in.
_RANK = {EXACT: 3, NORMALIZED: 2, FUZZY: 1, MISMATCH: 0}

MIN_FUZZY_CHARS = 12
LONG_QUOTE_CHARS = 40
RATIO_LONG = 0.92
RATIO_SHORT = 0.97
ANCHOR_WORDS = 3
# Bounds the difflib work per candidate: a common three-word anchor in a long
# source could otherwise produce hundreds of windows.
_MAX_WINDOWS = 40

# "…", "...", "[…]", "(...)" — an elision inside a quote.
_ELLIPSIS_RE = re.compile(r"\s*(?:\[\s*(?:…|\.{3,})\s*\]|\(\s*(?:…|\.{3,})\s*\)|…|\.{3,})\s*")
_QUOTE_EDGE = "\"'“”„‟«»‘’‚‛"

Candidate = str | tuple[str | None, str]


@dataclass(frozen=True)
class QuoteCheck:
    status: str
    # 1.0 for exact/normalized; the difflib ratio for fuzzy (and the best
    # ratio seen for a mismatch, 0.0 when no window was even anchored).
    score: float
    # The candidate that matched (its chunk ``text_hash``, when the caller
    # gave one).
    chunk_hash: str | None = None
    # [start, end) in that candidate's text (its NFC form — stored chunk text
    # always is) of what the quote matched.
    span: tuple[int, int] | None = None
    # The source's own wording of the matched passage; elided pieces are
    # joined with " … ". None for a mismatch.
    canonical: str | None = None


def rank(status: str | None) -> int:
    return _RANK.get(status or "", -1)


def worst(statuses: Sequence[str]) -> str | None:
    """The weakest of several quote statuses (``None`` for none)."""
    return min(statuses, key=rank) if statuses else None


def split_fragments(quote: str) -> list[str]:
    """The quote's pieces between elisions, surrounding quote marks and
    leading/trailing ellipses removed."""
    q = (quote or "").strip().strip(_QUOTE_EDGE).strip()
    return [p.strip() for p in _ELLIPSIS_RE.split(q) if p and p.strip()]


def verify_quote(quote: str, candidates: Sequence[Candidate]) -> QuoteCheck:
    """Check ``quote`` against each candidate source text, best result wins.

    ``candidates`` are texts, or ``(chunk_hash, text)`` pairs — typically the
    cited chunk first, then that chunk joined with its neighbours.
    """
    fragments = split_fragments(quote)
    if not fragments:
        return QuoteCheck(MISMATCH, 0.0)
    best: QuoteCheck | None = None
    for cand in candidates:
        chunk_hash, text = (None, cand) if isinstance(cand, str) else cand
        if not text:
            continue
        check = _check(fragments, text, chunk_hash)
        if best is None or (rank(check.status), check.score) > (rank(best.status), best.score):
            best = check
        if best.status == EXACT:
            break
    return best or QuoteCheck(MISMATCH, 0.0)


# ── one candidate ──────────────────────────────────────────────────────────


class _Source:
    """A candidate text plus its match-normalized form and the offset map
    back into the original (so a normalized or fuzzy hit can return the
    source's own slice)."""

    def __init__(self, text: str) -> None:
        self.text = text  # already NFC (see _check)
        self.norm, self.starts, self.ends = _normalize_with_map(text)
        # The per-character map must agree with the real normaliser; on the
        # rare text where it does not (a casefold whose output recomposes
        # with its neighbour), matching still uses the real form and only the
        # canonical slice is given up.
        real = normalize_for_match(text)
        if real != self.norm:
            self.norm, self.starts, self.ends = real, [], []
        self.has_marks = _strip_marks(self.norm) != self.norm

    def norm_pos(self, orig_pos: int) -> int:
        if not self.starts:
            return 0
        return bisect.bisect_left(self.starts, orig_pos)

    def orig_span(self, ks: int, ke: int) -> tuple[int, int] | None:
        if not self.starts or ke <= ks:
            return None
        return self.starts[ks], self.ends[ke - 1]


def _check(fragments: list[str], text: str, chunk_hash: str | None) -> QuoteCheck:
    # Offsets are into the NFC form (which stored chunk text already is), so
    # exact and normalized hits index the same string.
    if not unicodedata.is_normalized("NFC", text):
        text = unicodedata.normalize("NFC", text)
    src: _Source | None = None
    pos = 0          # original offset the next fragment must start at or after
    status = EXACT
    score = 1.0
    spans: list[tuple[int, int] | None] = []
    for frag in fragments:
        idx = text.find(frag, pos)
        if idx >= 0:
            spans.append((idx, idx + len(frag)))
            pos = idx + len(frag)
            continue
        if src is None:
            src = _Source(text)
        nfrag = normalize_for_match(frag)
        if not nfrag:
            continue
        kpos = src.norm_pos(pos)
        k = src.norm.find(nfrag, kpos)
        if k >= 0:
            span = src.orig_span(k, k + len(nfrag))
            spans.append(span)
            pos = span[1] if span else pos
            status = min(status, NORMALIZED, key=rank)
            continue
        hit = _fuzzy(nfrag, src, kpos)
        if hit is None:
            return QuoteCheck(MISMATCH, 0.0)
        ratio, ks, ke, ok = hit
        if not ok:
            return QuoteCheck(MISMATCH, ratio)
        span = src.orig_span(ks, ke)
        spans.append(span)
        pos = span[1] if span else pos
        status = min(status, FUZZY, key=rank)
        score = min(score, ratio)
    if not spans:
        return QuoteCheck(MISMATCH, 0.0)
    if any(s is None for s in spans):
        return QuoteCheck(status, score, chunk_hash)
    canonical = " … ".join(text[a:b] for a, b in spans)  # type: ignore[misc]
    return QuoteCheck(status, score, chunk_hash, (spans[0][0], spans[-1][1]), canonical)  # type: ignore[index]


def _fuzzy(nq: str, src: _Source, start: int) -> tuple[float, int, int, bool] | None:
    """Best anchored window for ``nq`` in ``src.norm`` at or after ``start``:
    ``(ratio, start, end, acceptable)``, or None when neither anchor occurs."""
    if len(nq) < MIN_FUZZY_CHARS:
        return None
    ns = src.norm
    words = nq.split(" ")
    head = " ".join(words[:ANCHOR_WORDS])
    tail = " ".join(words[-ANCHOR_WORDS:])
    n = len(nq)
    slack = max(4, n // 10)
    windows: set[tuple[int, int]] = set()

    for s in _find_all(ns, head, start):
        # Ends: wherever the tail anchor closes the window within reach, plus
        # the quote's own length give or take — the tail itself may be the
        # part the model changed.
        reach = ns[s: s + n + 2 * slack + len(tail)]
        ends = [s + e + len(tail) for e in _find_all(reach, tail, 0)]
        for e in ends + [s + n - slack, s + n, s + n + slack]:
            windows.add((s, min(len(ns), e)))
        if len(windows) >= _MAX_WINDOWS:
            break
    for t in _find_all(ns, tail, start):
        e = t + len(tail)
        for s in (e - n - slack, e - n, e - n + slack):
            windows.add((max(start, s), e))
        if len(windows) >= 2 * _MAX_WINDOWS:
            break
    if not windows:
        return None

    limit = RATIO_LONG if n >= LONG_QUOTE_CHARS else RATIO_SHORT
    best: tuple[float, int, int, bool] | None = None
    for s, e in sorted({_snap(ns, s, e) for s, e in windows}):
        if e <= s:
            continue
        win = ns[s:e]
        sm = difflib.SequenceMatcher(None, nq, win, autojunk=False)
        # quick_ratio is an upper bound; skip windows that cannot win.
        if best is not None and sm.quick_ratio() <= best[0]:
            continue
        ratio = sm.ratio()
        ok = ratio >= limit and _same_digits(nq, win) and not (
            src.has_marks and _diacritics_only_difference(sm, nq, win)
        ) and not _changes_words(nq, win)
        cand = (ratio, s, e, ok)
        # An acceptable window beats a better-scoring unacceptable one.
        if best is None or (ok, ratio) > (best[3], best[0]):
            best = cand
    return best


def _snap(ns: str, s: int, e: int) -> tuple[int, int]:
    """Widen a window to whole words, so the canonical slice never starts or
    ends mid-word (a length-based end usually lands inside one)."""
    s, e = max(0, s), min(len(ns), e)
    while 0 < s < len(ns) and ns[s - 1].isalnum() and ns[s].isalnum():
        s -= 1
    while 0 < e < len(ns) and ns[e - 1].isalnum() and ns[e].isalnum():
        e += 1
    return s, e


def _find_all(hay: str, needle: str, start: int) -> list[int]:
    out: list[int] = []
    if not needle:
        return out
    i = hay.find(needle, start)
    while i >= 0 and len(out) < _MAX_WINDOWS:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def _same_digits(a: str, b: str) -> bool:
    return sorted(c for c in a if c.isdigit()) == sorted(c for c in b if c.isdigit())


def _strip_marks(s: str) -> str:
    """Diacritics removed (``đ`` → ``d``); case and spacing untouched, unlike
    :func:`~app.userdocs.textnorm.fold`, so a segment comparison can't mistake
    a whitespace change for an accent change."""
    t = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return t.replace("đ", "d").replace("Đ", "D")


def _diacritics_only_difference(sm: difflib.SequenceMatcher, a: str, b: str) -> bool:
    """True when some differing stretch between ``a`` and ``b`` differs only
    in diacritics — the one change fuzzy matching must never absorb."""
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        x, y = a[i1:i2], b[j1:j2]
        if x and y and x != y and _strip_marks(x) == _strip_marks(y):
            return True
    return False


# Two words closer than this are the same word misspelled; further apart,
# one word was put in place of another.
_SAME_WORD_RATIO = 0.75
# Words whose omission reverses or narrows what a passage says. A quote may
# leave source words out ("Tòa án nhân dân giải quyết" → "Tòa án giải
# quyết"), but never one of these ("không được chuyển nhượng" → "được chuyển
# nhượng"). Compared in match-normalised (casefolded) form.
_MEANING_WORDS = frozenset({
    # Vietnamese: not / not yet / never / forbidden / except / only / unless
    "không", "chưa", "chẳng", "chớ", "đừng", "cấm", "trừ", "ngoại", "chỉ", "nếu", "miễn",
    # English
    "not", "no", "never", "nor", "neither", "cannot", "except", "excluding", "unless", "without",
    "only", "if", "provided", "notwithstanding",
})


def _changes_words(quote: str, window: str) -> bool:
    """True when the quote differs from the source window by a *word*, not a
    spelling: a word replaced by another ("do Tòa án giải quyết" quoted as
    "do UBND giải quyết"), a word the source does not have, or a dropped
    negation/exception/restriction word (see ``_MEANING_WORDS``). Those
    change what the passage says, however similar the characters are.
    Typos inside a word, and ordinary words left out, do not. Words at the
    very edges of the window are its slack, not the quote's."""
    qw, ww = quote.split(), window.split()
    sm = difflib.SequenceMatcher(None, qw, ww, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        at_edge = (i1 == 0 and j1 == 0) or (i2 == len(qw) and j2 == len(ww))
        if tag == "delete":
            # Words in the quote the source does not have.
            if not at_edge:
                return True
            continue
        if tag == "insert":
            # Source words the quote left out.
            if not at_edge and any(w.strip(".,;:!?()\"'") in _MEANING_WORDS for w in ww[j1:j2]):
                return True
            continue
        a, b = qw[i1:i2], ww[j1:j2]
        if len(a) != len(b):
            # "khởikiện" vs "khởi kiện": a split or joined word is still a typo.
            if difflib.SequenceMatcher(None, "".join(a), "".join(b)).ratio() < _SAME_WORD_RATIO:
                return True
            continue
        for x, y in zip(a, b):
            if difflib.SequenceMatcher(None, x, y).ratio() < _SAME_WORD_RATIO:
                return True
    return False


def _normalize_with_map(text: str) -> tuple[str, list[int], list[int]]:
    """:func:`normalize_for_match` applied per character, recording for every
    output character the original ``[start, end)`` it came from."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, ch in enumerate(text):
        # Any whitespace is one separating space (collapsed below); a dropped
        # character (soft hyphen, zero-width) yields "" and contributes nothing.
        piece = " " if ch.isspace() else normalize_for_match(ch)
        for p in piece:
            if p == " " and (not out or out[-1] == " "):
                continue
            out.append(p)
            starts.append(i)
            ends.append(i + 1)
    while out and out[-1] == " ":
        out.pop()
        starts.pop()
        ends.pop()
    return "".join(out), starts, ends
