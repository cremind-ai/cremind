"""Keyword ranking for the Cremind manual, merged into its vector ranking.

The manual is ranked by embedding each page's ``<name>\\n\\n<description>``
(:func:`app.cremind_documents.sync._embedding_text`) against the query, and only
the top ``k`` pages reach the relevance judge. Every ``[cli]`` page's
description is dense with the same words — "Cremind", "CLI", "list",
"configure", a run of `` `cremind …` `` commands — and the embedding of a query
phrased the way the agent phrases it ("list all configured Cremind LLM
providers CLI command") is dominated by exactly those words. The page that owns
the one distinctive word ("LLM") ranked 22nd of 51, below the judge's cut, so
the search answered "no relevant result" with the right page in the library.

BM25 weighs a word by how few pages contain it, so "llm" outweighs "cremind",
"cli" and "command" by construction. :func:`merge` lets the two rankings take
turns filling the candidate list, so the best keyword matches reach the judge
whatever the vectors thought of them.

A page is ranked only through an *informative* query word — one that at most
half of the pages contain. A query that shares only common words with the
manual, or none (most Vietnamese queries: the manual is written in English), is
ranked by the vectors alone, exactly as before this module existed.

Pure: no I/O, and nothing imported beyond :mod:`app.cremind_documents.sections`.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Callable, Hashable, Sequence

from app.cremind_documents.sections import query_words

__all__ = ["NAME_WEIGHT", "merge", "rank", "tokens"]

# Okapi BM25's customary constants.
_K1 = 1.2
_B = 0.75
# A page's name is its identity ("llm", "calendar"): a query word that names
# the page counts as this many mentions in its text.
NAME_WEIGHT = 3

# Words that say nothing about WHICH page is meant: English function words,
# their Vietnamese counterparts, and the filler of a request ("please show me
# …"). Without this, a function word that happens to be rare among the
# descriptions ("how" opens only document.md's) would rank that page.
_STOP_WORDS = frozenset({
    "a", "about", "all", "also", "an", "and", "any", "are", "as", "at", "be",
    "been", "but", "by", "can", "could", "did", "do", "does", "each", "every",
    "for", "from", "give", "had", "has", "have", "help", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "my", "need", "not", "of", "on",
    "or", "our", "please", "should", "show", "so", "some", "tell", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "too", "us", "via", "want", "was", "we", "were", "what",
    "when", "where", "which", "who", "why", "will", "with", "would", "you",
    "your",
    "và", "của", "cho", "trên", "để", "là", "có", "không", "các", "những",
    "trong", "với", "một", "khi", "nào", "gì", "thế", "như", "được", "này",
    "đó", "bằng", "cách", "tôi", "bạn", "hãy", "giúp", "xin", "mình", "thì",
    "mà", "từ", "đến", "về", "hoặc", "nếu", "sao", "ra", "vào", "tất", "cả",
})


def tokens(text: str) -> list[str]:
    """The words of ``text`` as keyword ranking compares them.

    Lower-cased, with backticks dropped and hyphens split (``set-args`` →
    ``set``, ``args``) exactly as section lookup splits them; stop words and
    single characters removed; each word reduced to a crude stem
    (:func:`_stem`).
    """
    out: list[str] = []
    for word in query_words(text):
        if word in _STOP_WORDS:
            continue
        word = _stem(word)
        if len(word) > 1:
            out.append(word)
    return out


# A doubled final consonant left by cutting "-ed"/"-ing" is undoubled
# ("embedded" → "embed"), except these, which English doubles in the stem
# itself ("installed" → "install").
_KEEP_DOUBLED = frozenset("lsz")


def _stem(word: str) -> str:
    """A light English stemmer, so inflections of one word compare equal:
    ``providers`` → ``provider``, ``configured``/``configure``/``configuring``
    → ``configur``, ``listed``/``lists`` → ``list``, ``entries`` → ``entry``.

    Crude on purpose — it only has to be consistent, since the query and the
    pages go through the same function. Without it, the one page that happened
    to say "configured" owned that word, and a query saying "configured" ranked
    it above the page the query was about.
    """
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            if word[-1] == word[-2] and word[-1] not in _KEEP_DOUBLED and word[-1].isalpha():
                word = word[:-1]
            return word
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def rank(query: str, corpus: Sequence[tuple[str, str]]) -> list[tuple[int, float]]:
    """Rank ``corpus`` — one ``(name, text)`` pair per page — against ``query``.

    Returns ``(index, score)`` for every page that contains at least one
    informative query word (see the module docstring), best first; ties keep
    corpus order. Empty when no query word is informative.
    """
    words = list(dict.fromkeys(tokens(query)))
    if not words or not corpus:
        return []
    docs = [tokens(name) * NAME_WEIGHT + tokens(text) for name, text in corpus]
    n = len(docs)
    avg_len = (sum(len(d) for d in docs) / n) or 1.0
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
    informative = {w for w in words if 0 < df[w] and 2 * df[w] <= n}
    if not informative:
        return []

    ranked: list[tuple[int, float]] = []
    for index, doc in enumerate(docs):
        tf = Counter(doc)
        if not any(tf[w] for w in informative):
            continue
        norm = _K1 * (1 - _B + _B * len(doc) / avg_len)
        score = 0.0
        for w in words:
            f = tf[w]
            if f:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * f * (_K1 + 1) / (f + norm)
        ranked.append((index, score))
    ranked.sort(key=lambda row: -row[1])
    return ranked


def merge(
    dense: Sequence[dict],
    keyword: Sequence[dict],
    limit: int,
    *,
    key: Callable[[dict], Hashable],
) -> list[dict]:
    """The candidates the judge sees: at most ``limit`` pages.

    The two rankings take turns, vector first, each adding its best page not
    yet in (a team draft). The vector ranking's top page leads as it always
    has, and the keyword ranking places at most half of the pages — none when
    ``limit`` is 1. When one ranking runs out, the other fills the remaining
    seats. A page both rankings return comes back as its vector hit (it
    carries the score). With no keyword hits the result is ``dense[:limit]``,
    unchanged.
    """
    if not keyword:
        return list(dense[:limit])

    by_key: dict[Hashable, dict] = {}
    for hit in (*dense, *keyword):
        by_key.setdefault(key(hit), hit)

    queues = [[key(h) for h in dense], [key(h) for h in keyword]]
    positions = [0, 0]
    picked: list[Hashable] = []
    seen: set[Hashable] = set()
    turn = 0
    while len(picked) < limit:
        for side in (turn, 1 - turn):
            queue = queues[side]
            while positions[side] < len(queue) and queue[positions[side]] in seen:
                positions[side] += 1
            if positions[side] < len(queue):
                k = queue[positions[side]]
                picked.append(k)
                seen.add(k)
                break
        else:
            break
        turn = 1 - turn
    return [by_key[k] for k in picked]
