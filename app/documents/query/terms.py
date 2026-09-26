"""From a user's query to safe full-text queries.

Two things go wrong with naive full-text search over a user's own files, and
this module exists to prevent both:

- **Injection.** FTS5 has its own query language (``AND``, ``NEAR``, ``*``,
  column filters, ``^``). A query such as ``Luật "đất đai" NEAR/3 ABC*`` must
  search for those words, never execute that syntax — and a stray quote must
  never turn into a syntax error the agent sees as "nothing found". Every term
  is therefore emitted as a double-quoted string (inner quotes doubled), and
  every column filter is one of our own constants.
- **Diacritics.** Vietnamese is often typed without accents ("luat dat dai"),
  while the index tokenises with ``remove_diacritics 0`` so that "đất" (land)
  and "dat" stay different words. An unaccented term therefore also searches
  the chunk's ``folded`` copy, with equal weight; an accented term searches the
  exact text and, at the lower weight bm25 gives that column, the folded copy.

Identifiers and legal references ("45/2013/QH13", "Điều 203", "MKT-report")
lose their meaning when split into loose words, so they also go into a
separate *phrase* query that fusion weighs above the word query.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.documents.textnorm import fold

# What FTS5's unicode61 tokenizer keeps together: letters and digits. The
# underscore is punctuation to it, so "word3_7" is two tokens there and here.
_WORD_RE = re.compile(r"[^\W_]+")
# A user's own quoted phrase, straight or typographic quotes.
_QUOTED_RE = re.compile(r'"([^"]{2,200})"|“([^”]{2,200})”')
# Words glued by / - . _ that mean one thing only together: a law number
# (45/2013/QH13), a folder name (MKT-report), a version (v1.2), a file name.
_IDENT_RE = re.compile(r"(?<![\w/.-])[^\W_]+(?:[/._-][^\W_]+)+(?![\w/])")
# "Điều 203", "khoản 2", "Article 5", "Chương II" — the unit a legal question
# is really about.
_LEGAL_REF_RE = re.compile(
    r"(?<!\w)(điều|dieu|khoản|khoan|điểm|diem|chương|chuong|mục|muc|phần|phan|"
    r"article|art\.|section|chapter|clause|part)\s+(\d+[a-z]?|[ivxlc]+)(?!\w)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")

# Letters that exist only in Vietnamese (among the Latin scripts people type
# here): their presence settles the language question.
_VI_LETTERS = set("ăâđêôơưĂÂĐÊÔƠƯ")
# Common Vietnamese syllables typed without accents. A query made mostly of
# these is Vietnamese without diacritics, which thorough mode can restore.
_VI_NODIAC = frozenset(
    "luat dat dai hop dong cua cho nhung nguoi viec lam khach hang cong ty bao cao "
    "ket qua kinh doanh thang nam ngay tai lieu thong tin quyen nghia vu tranh chap "
    "giai phap du an nghi dinh thong tu quyet dinh chuong dieu khoan diem muc van ban "
    "anh chup hinh cho con cong vien ve voi cac mot hai ba".split()
)

# Function words that only dilute an OR-query's ranking. The Vietnamese ones
# are listed both accented and folded, because either may be typed.
STOP_WORDS = frozenset(
    "a an and any are as at be by can could do does for from how i if in into is it its "
    "me my of on or our should so that the their them then there these this to via was "
    "we what when where which who why will with would you your about find show file files "
    "document documents doc docs wrote written".split()
    + "và va của cua cho trên tren để de là la có co không khong các cac những nhung "
      "trong với voi một mot khi nào nao gì gi thế the như nhu được duoc này nay đó do "
      "bằng bang cách cach tìm tim về ve".split()
)

# Column sets. ``text`` always counts; the heading breadcrumb is searchable
# too (bm25 weighs it low); ``folded`` is the accent-free copy.
ALL_COLUMNS = "{heading text folded}"
EXACT_COLUMNS = "{heading text}"
FOLDED_COLUMN = "folded"

MAX_TERMS = 24
MAX_PHRASES = 8


def quote(term: str) -> str:
    """``term`` as an FTS5 string: double-quoted, inner quotes doubled — the
    only form in which user text ever reaches a MATCH expression."""
    return '"' + term.replace('"', '""') + '"'


def _has_diacritics(word: str) -> bool:
    return fold(word) != word.casefold()


@dataclass(frozen=True)
class QueryTerms:
    """What a query contains, as the lexical and fusion stages need it."""

    raw: str
    # Casefolded word tokens, stop words removed, in order, unique.
    tokens: tuple[str, ...]
    # Phrases searched as a unit: quoted text, identifiers, legal references.
    phrases: tuple[str, ...]
    identifiers: tuple[str, ...]
    numbers: tuple[str, ...]
    has_diacritics: bool
    # vi (accented Vietnamese), vi_nodiac (Vietnamese typed without accents), en
    lang: str

    @property
    def has_ids_or_numbers(self) -> bool:
        """Identifiers and numbers are exactly what embeddings blur and
        keyword search pins down, so fusion trusts the lexical list more."""
        return bool(self.identifiers or self.numbers)

    @property
    def empty(self) -> bool:
        return not self.tokens and not self.phrases


def analyze(query: str) -> QueryTerms:
    """Split ``query`` into word tokens, phrases, identifiers and numbers."""
    q = unicodedata.normalize("NFC", query or "").strip()
    phrases: list[str] = []
    for m in _QUOTED_RE.finditer(q):
        phrases.append((m.group(1) or m.group(2) or "").strip())
    identifiers = [m.group(0) for m in _IDENT_RE.finditer(q)]
    legal = [f"{m.group(1)} {m.group(2)}" for m in _LEGAL_REF_RE.finditer(q)]
    numbers = [m.group(0) for m in _NUMBER_RE.finditer(q)]
    for p in identifiers + legal:
        if p not in phrases:
            phrases.append(p)

    words = [w.casefold() for w in _WORD_RE.findall(q)]
    tokens: list[str] = []
    for w in words:
        if w in STOP_WORDS or (len(w) == 1 and not w.isdigit()) or w in tokens:
            continue
        tokens.append(w)
    if not tokens and words:
        # A query made only of stop words still means something ("what is
        # this"); searching nothing would be worse than searching them.
        tokens = list(dict.fromkeys(words))

    has_diacritics = any(_has_diacritics(w) for w in words)
    if any(ch in _VI_LETTERS for ch in q) or (has_diacritics and any(ord(ch) > 0x2FF for ch in q)):
        lang = "vi"
    elif words and sum(1 for w in words if w in _VI_NODIAC) * 2 >= len(words):
        lang = "vi_nodiac"
    else:
        lang = "en"
    return QueryTerms(
        raw=q,
        tokens=tuple(tokens[:MAX_TERMS]),
        phrases=tuple(p for p in phrases if _WORD_RE.search(p))[:MAX_PHRASES],
        identifiers=tuple(dict.fromkeys(identifiers)),
        numbers=tuple(dict.fromkeys(numbers)),
        has_diacritics=has_diacritics,
        lang=lang,
    )


def _term_clauses(words: list[str]) -> list[str]:
    """Column-filtered clauses for one term (a word, or a phrase given as its
    words). Unaccented: every column, the folded copy included. Accented: the
    exact text, plus the folded copy for text indexed from a source that
    lost its accents."""
    phrase = " ".join(words)
    folded = fold(phrase)
    if folded == phrase:
        return [f"{ALL_COLUMNS} : {quote(phrase)}"]
    return [f"{EXACT_COLUMNS} : {quote(phrase)}", f"{FOLDED_COLUMN} : {quote(folded)}"]


def build_match(terms: QueryTerms) -> str | None:
    """The word query: every token OR-ed, so bm25 ranks chunks that hold more
    (and rarer) terms first instead of requiring all of them. None when the
    query has no searchable word."""
    clauses: list[str] = []
    for tok in terms.tokens:
        clauses += _term_clauses([tok])
    return " OR ".join(clauses) or None


def build_phrase_match(terms: QueryTerms) -> str | None:
    """The phrase query: identifiers, legal references and quoted text, each
    as an exact phrase (FTS5 tokenises a quoted string into a phrase, so
    "45/2013/QH13" matches the tokens 45, 2013, qh13 in that order)."""
    clauses: list[str] = []
    for ph in terms.phrases:
        words = [w.casefold() for w in _WORD_RE.findall(ph)]
        if words:
            clauses += _term_clauses(words)
    return " OR ".join(dict.fromkeys(clauses)) or None


def like_terms(terms: QueryTerms) -> list[str]:
    """Substrings for the LIKE fallback (an index without FTS5): the tokens
    and their folded forms, so an unaccented query still finds accented text
    through the ``folded`` column."""
    out: list[str] = []
    for tok in terms.tokens:
        for t in (tok, fold(tok)):
            if t and t not in out:
                out.append(t)
    return out


def matches_text(terms: QueryTerms, text: str) -> int:
    """How many distinct query tokens occur in ``text`` (accent-insensitive)
    — for choosing a snippet window and ranking reader sections."""
    hay = fold(text or "")
    return sum(1 for tok in terms.tokens if fold(tok) in hay)


__all__ = [
    "ALL_COLUMNS",
    "QueryTerms",
    "STOP_WORDS",
    "analyze",
    "build_match",
    "build_phrase_match",
    "like_terms",
    "matches_text",
    "quote",
]
