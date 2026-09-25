"""Text normalisation shared by the chunker, the chunk diff and the query side.

Three different notions of "the same text" live here, and mixing them up is
the bug this module exists to prevent:

- :func:`normalize_ws` is **identity**. A chunk's ``text_hash`` is taken over
  it, so two extractions that differ only in line endings, trailing blanks or
  NFC/NFD form hash equal and the diff keeps the old vector instead of paying
  for a new embedding. It never touches letters, so it cannot merge two texts
  that mean different things.
- :func:`fold` is **recall**. Vietnamese is routinely typed without diacritics
  ("luat dat dai" for "Luật Đất đai"), and FTS5's ``unicode61`` tokenizer runs
  with ``remove_diacritics 0`` so accented terms keep their full weight. The
  folded copy of a chunk is what an unaccented query matches against. ``đ`` is
  not a base letter plus a combining mark (it is its own code point), so NFD
  alone would leave it; it is mapped by hand.
- :func:`normalize_for_match` is **quote verification**. An LLM that quotes a
  passage rewrites typography (curly quotes, dashes, ellipses, soft hyphens
  from PDF extraction) but must not be allowed to drop diacritics: in
  Vietnamese that changes the word ("đất" is land, "đát" is not).

:func:`estimate_tokens` sizes chunks without loading a tokenizer. It is
deliberately pessimistic: the default embedder (multilingual-e5-base) has a
512-token window and sentence-transformers truncates longer input *silently*,
so an under-estimate would lose the tail of a chunk from its vector with no
error anywhere. Over-estimating only costs a few more, smaller chunks.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata

# ── normalize_ws ───────────────────────────────────────────────────────────

# Horizontal whitespace that PDF/DOCX extraction produces interchangeably with
# a plain space (NBSP, the typographic spaces, ideographic space, form feed,
# vertical tab). Treating them as spaces keeps a re-extraction that swaps one
# for another from changing every hash in the file.
_HSPACE_ODD = "\\t\\f\\v\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000"
_HSPACE = f"[ {_HSPACE_ODD}]"
_TRAILING_WS_RE = re.compile(f"{_HSPACE}+(?=\\n|$)")
# A run of two or more, or a single non-plain space. A lone plain space — by
# far the most common match of a naive "{_HSPACE}+" — is left alone, which
# keeps this hot path (every chunk hash goes through it) from rewriting
# every word gap in the text.
_HSPACE_RUN_RE = re.compile(f"{_HSPACE}{{2,}}|[{_HSPACE_ODD}]")
_MANY_NL_RE = re.compile(r"\n{3,}")


def normalize_ws(text: str) -> str:
    """NFC; CRLF/CR→LF; drop trailing blanks per line; collapse runs of
    spaces/tabs to one space; collapse 3+ newlines to 2; strip the ends.

    Stripping the ends is part of identity: a paragraph is the same paragraph
    whether or not the extractor left a trailing newline on it.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text)
    if "\r" in t:
        t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = _TRAILING_WS_RE.sub("", t)
    t = _HSPACE_RUN_RE.sub(" ", t)
    if "\n\n\n" in t:
        t = _MANY_NL_RE.sub("\n\n", t)
    return t.strip()


# ── fold ───────────────────────────────────────────────────────────────────

class _FoldTable(dict):
    """``str.translate`` table: combining marks (Mn) → deleted, ``đ``/``Đ`` →
    ``d``, everything else → itself. Filled lazily per code point, so the
    per-character work runs in C after a character's first sighting (a
    generator over ``unicodedata.category`` made folding the chunker's
    single most expensive step). Concurrent fills are benign: every thread
    computes the same value."""

    def __missing__(self, cp: int) -> str | None:
        ch = chr(cp)
        val = None if unicodedata.category(ch) == "Mn" else ch
        self[cp] = val
        return val


_FOLD_TABLE = _FoldTable({ord("đ"): "d", ord("Đ"): "d"})


def fold(text: str) -> str:
    """Diacritics- and case-folded text for accent-insensitive keyword search.

    NFD, drop combining marks (category Mn), map ``đ``/``Đ`` to ``d``,
    casefold, then :func:`normalize_ws` (whose NFC recomposes anything the
    NFD split that is not a mark, e.g. Hangul syllables).
    ``fold("Luật Đất đai") == "luat dat dai"``.
    """
    if not text:
        return ""
    if text.isascii():
        return normalize_ws(text.casefold())
    t = unicodedata.normalize("NFD", text).translate(_FOLD_TABLE).casefold()
    return normalize_ws(t)


def fold_if_needed(text: str) -> str | None:
    """:func:`fold` of ``text``, or ``None`` when folding changes nothing
    beyond case — an English chunk then stores no second copy of itself."""
    if not text or text.isascii():
        return None  # nothing to fold in pure ASCII
    folded = fold(text)
    if folded == normalize_ws(text).casefold():
        return None
    return folded


# ── normalize_for_match ────────────────────────────────────────────────────

_MATCH_MAP = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u00ab": '"', "\u00bb": '"', "\uff02": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2039": "'", "\u203a": "'", "\uff07": "'",
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2015": "-",
    "\u2026": "...",
    # Invisible characters PDF and web extraction leave inside words.
    "\u00ad": None, "\u200b": None, "\u200c": None, "\u200d": None,
    "\u2060": None, "\ufeff": None,
})
_ANY_WS_RE = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """Canonical form for checking that a quote really occurs in a source.

    NFC, casefold, unify quotes/dashes/ellipsis, drop soft hyphens and
    zero-width characters, collapse all whitespace (newlines included) to one
    space. Diacritics are kept on purpose — see the module docstring.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFC", text).casefold()
    t = t.translate(_MATCH_MAP)
    # casefold can emit non-NFC sequences ("İ" becomes "i" + U+0307); re-compose.
    t = unicodedata.normalize("NFC", t)
    return _ANY_WS_RE.sub(" ", t).strip()


# ── estimate_tokens ────────────────────────────────────────────────────────

# Scripts that SentencePiece/XLM-R tokenises at roughly one token per
# character (or worse): Han, kana, Hangul, Thai, Lao, Khmer, Myanmar, plus the
# CJK punctuation and full-width blocks. Their combining marks sit inside
# these ranges, so they are counted too — an intended over-count.
_DENSE_RANGES = (
    "\u0e00-\u0eff"          # Thai, Lao
    "\u1000-\u109f"          # Myanmar
    "\u1100-\u11ff"          # Hangul Jamo
    "\u1780-\u17ff"          # Khmer
    "\u2e80-\u2fdf"          # CJK radicals
    "\u3000-\u303f"          # CJK symbols and punctuation
    "\u3040-\u30ff"          # Hiragana, Katakana
    "\u3100-\u31ff"          # Bopomofo, Hangul compatibility Jamo, kana ext.
    "\u3200-\u4dbf"          # enclosed CJK, CJK Ext A
    "\u4e00-\u9fff"          # CJK Unified Ideographs
    "\ua960-\ua97f"          # Hangul Jamo Ext A
    "\uac00-\ud7ff"          # Hangul syllables, Jamo Ext B
    "\uf900-\ufaff"          # CJK compatibility ideographs
    "\uff00-\uffef"          # half/full-width forms
    "\U00020000-\U0003ffff"  # CJK Ext B and beyond
)
_DENSE_RE = re.compile(f"[{_DENSE_RANGES}]")
# Combining marks are not ``\w`` in Python's ``re`` (they are not
# alphanumeric), yet in decomposed text they belong to the word they sit on.
_MARKS = "\u0300-\u036f\u0483-\u0489\u0591-\u05bd\u0610-\u061a\u064b-\u065f" \
         "\u0900-\u0903\u093a-\u094f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
_MARK_RE = re.compile(f"[{_MARKS}]")
_WORD_CH = f"(?:[^\\W_]|[{_MARKS}])"
_WORD_RE = re.compile(f"{_WORD_CH}+")
# Same as _WORD_RE for text without combining marks (all NFC Vietnamese,
# English, …) and several times faster: no alternation per character.
_WORD_FAST_RE = re.compile(r"[^\W_]+")
_PUNCT_RE = re.compile(f"[^\\w\\s{_MARKS}]|_")
_NL_RUN_RE = re.compile(r"\s*\n\s*")
# Every cased uppercase letter below U+2000 (Latin incl. the Vietnamese
# precomposed capitals, Greek, Cyrillic), built once so the surcharge below is
# a single C-level regex pass instead of a Python loop per word.
_UPPER_CLASS = "".join(
    re.escape(c) for c in map(chr, range(0x41, 0x2000)) if c.isupper() and len(c) == 1
)
_INNER_UPPER_RE = re.compile(f"(?<={_WORD_CH})[{_UPPER_CLASS}]")
_INNER_UPPER_FAST_RE = re.compile(f"(?<=[^\\W_])[{_UPPER_CLASS}]")
_DIGIT_RUN_RE = re.compile(r"\d{3,}")

# Calibrated against the multilingual-e5-base (XLM-R SentencePiece) tokenizer
# on ~4,500 paragraphs: this repo's Markdown docs, Python and TypeScript
# sources, and synthetic Vietnamese/English/mixed/ALL-CAPS/identifier-heavy
# prose. With these values fewer than 0.5% of paragraphs in any set are
# under-counted (none of the vi/en prose ones), and the estimate runs ~1.3-1.5×
# the real count on prose — the price of never silently truncating a chunk.
#
# Base rates. A Vietnamese syllable or a common English word is one piece, so
# 1.4 per word already over-counts plain prose.
WORD_COST = 1.4
DENSE_COST = 1.0
NEWLINE_RUN_COST = 1.0
# Punctuation is charged a whole token, not the 0.5 the design first assumed:
# XLM-R splits nearly every mark off on its own ("▁`", "-", "-", "j", "son",
# "`"), and at 0.5 a third of Markdown paragraphs and over half of code
# paragraphs came out under-counted.
PUNCT_COST = 1.0
# Surcharges for word shapes where 1.4 per word under-counts: upper case after
# a word's first letter ("CỘNG HÒA XÃ HỘI" is ~2 pieces per syllable,
# "getUserById" is 6), long digit runs ("0987654321" is 5), and long words
# (identifiers, hashes, rare compounds: "indemnification" is 3).
# Each only adds, so the estimate never drops below the base rule. Each is
# also shaped so that cutting a word in two never lowers the total by more
# than the 1.4 the extra word adds (``LONG_WORD_CHARS / LONG_WORD_CHARS_PER_TOKEN
# <= WORD_COST``); the chunker's Gear split relies on that.
INNER_UPPER_COST = 0.8
DIGIT_COST = 0.5          # per digit beyond the second in a run
LONG_WORD_CHARS = 5
LONG_WORD_CHARS_PER_TOKEN = 3.6


def estimate_tokens(text: str) -> int:
    """Model-independent, deliberately high token count for ``text``.

    1 per CJK/Thai/Hangul-class character; 1.4 per word of any other script
    (a word is a run of letters/digits including combining marks); 1 per
    punctuation or symbol character; 1 per run of newlines; plus the shape
    surcharges above; rounded up.

    Additive: ``estimate(a + "\\n\\n" + b) <= estimate(a) + estimate(b) + 1``,
    which is what lets the chunker size a chunk from its blocks' estimates
    without re-counting the joined text.
    """
    if not text:
        return 0
    rest, dense = (text, 0) if _DENSE_RE.search(text) is None else _DENSE_RE.subn(" ", text)
    if _MARK_RE.search(rest) is None:
        words = _WORD_FAST_RE.findall(rest)
        inner_upper = len(_INNER_UPPER_FAST_RE.findall(rest))
    else:
        words = _WORD_RE.findall(rest)
        inner_upper = len(_INNER_UPPER_RE.findall(rest))
    total = dense * DENSE_COST
    total += len(words) * WORD_COST
    total += sum(len(w) - LONG_WORD_CHARS for w in words if len(w) > LONG_WORD_CHARS) \
        / LONG_WORD_CHARS_PER_TOKEN
    total += inner_upper * INNER_UPPER_COST
    total += len(_PUNCT_RE.findall(rest)) * PUNCT_COST
    total += len(_NL_RUN_RE.findall(rest)) * NEWLINE_RUN_COST
    total += sum(len(m) - 2 for m in _DIGIT_RUN_RE.findall(rest)) * DIGIT_COST
    return math.ceil(total - 1e-9)


# ── text_hash ──────────────────────────────────────────────────────────────


def text_hash(heading: str, text: str) -> str:
    """blake2b-128 hex over ``normalize_ws(heading + "\\n" + text)`` (just the
    text when there is no heading) — exactly the string that gets embedded,
    so equal hashes mean an existing vector can be reused."""
    raw = f"{heading}\n{text}" if heading else text
    return hashlib.blake2b(normalize_ws(raw).encode("utf-8"), digest_size=16).hexdigest()


__all__ = [
    "estimate_tokens",
    "fold",
    "fold_if_needed",
    "normalize_for_match",
    "normalize_ws",
    "text_hash",
]
