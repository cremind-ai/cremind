"""Query text → FTS5 expressions: never injectable, never a syntax error.

The agent's query is user text; FTS5 has its own query language. Whatever the
query contains — operators, quotes, stars, colons, parentheses, carets, a
column filter lookalike — the built expression must search for words and
nothing else, and SQLite must accept it.
"""

from __future__ import annotations

import random
import sqlite3

import pytest

from app.userdocs.query.terms import (
    ALL_COLUMNS,
    analyze,
    build_match,
    build_phrase_match,
    like_terms,
    matches_text,
    quote,
)


@pytest.fixture(scope="module")
def fts():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(heading, text, folded, "
                     "tokenize='unicode61 remove_diacritics 0')")
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no FTS5")
    rows = [
        ("Điều 203", "Luật đất đai số 45/2013/QH13 quy định về tranh chấp",
         "luat dat dai so 45/2013/qh13 quy dinh ve tranh chap"),
        ("", "The AI challenges report: hallucination AND privacy", None),
        ("MKT-report", "Business results NEAR record revenue", None),
    ]
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    yield conn
    conn.close()


def _run(conn, match):
    return conn.execute("SELECT rowid FROM t WHERE t MATCH ? ORDER BY bm25(t, 0.3, 1.0, 0.8)", (match,)).fetchall()


def test_quote_doubles_inner_quotes():
    assert quote('a"b') == '"a""b"'
    assert quote("") == '""'


def test_every_term_is_quoted_and_columns_are_ours():
    terms = analyze('AI AND NEAR(x y) "privacy" col:hack -minus star* ^caret')
    match = build_match(terms)
    assert match is not None
    for clause in match.split(" OR "):
        cols, _, term = clause.partition(" : ")
        assert cols in (ALL_COLUMNS, "{heading text}", "folded")
        assert term.startswith('"') and term.endswith('"')


@pytest.mark.parametrize("query", [
    'AI AND privacy', 'NEAR(a b, 3)', 'a OR b NOT c', '"unterminated', 'col:value', 'star*', '-minus',
    '^start', '(paren', 'a:b:c', '"" "" ""', "it's", '{heading}: x', 'Điều 203 "khoản 2"',
    '45/2013/QH13', 'MKT-report', 'x' * 500, '***', ':::', '"', 'a"b"c', 'NEAR/3',
])
def test_hostile_queries_are_accepted_by_sqlite(fts, query):
    terms = analyze(query)
    for match in (build_match(terms), build_phrase_match(terms)):
        if match:
            _run(fts, match)  # must not raise


def test_fuzzed_queries_never_raise(fts):
    rnd = random.Random(20260925)
    alphabet = list('abcxyzđĐấẬ0123456789 "\'*:^-+()[]{}/\\.,;!?_') + ["AND", "OR", "NOT", "NEAR", " "]
    for _ in range(400):
        q = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 40)))
        terms = analyze(q)
        for match in (build_match(terms), build_phrase_match(terms)):
            if match:
                _run(fts, match)


def test_operators_are_searched_as_words(fts):
    # "AND" and "NEAR" appear literally in rows 2 and 3.
    assert _run(fts, build_match(analyze("hallucination AND"))) == [(2,)]
    assert (3,) in _run(fts, build_match(analyze("NEAR record")))


def test_unaccented_query_matches_accented_text_through_folded(fts):
    assert _run(fts, build_match(analyze("tranh chap dat dai")))[0] == (1,)


def test_accented_query_matches_the_exact_text(fts):
    assert _run(fts, build_match(analyze("tranh chấp")))[0] == (1,)


def test_identifiers_and_legal_references_become_phrases(fts):
    terms = analyze("Luật 45/2013/QH13 Điều 203 MKT-report")
    assert "45/2013/QH13" in terms.phrases
    assert any(p.lower().startswith("điều 203") for p in terms.phrases)
    assert "MKT-report" in terms.phrases
    assert terms.has_ids_or_numbers
    phrase = build_phrase_match(terms)
    assert phrase and _run(fts, phrase)
    assert _run(fts, build_phrase_match(analyze("MKT-report"))) == [(3,)]


def test_quoted_text_is_a_phrase():
    terms = analyze('the "business results" of Q2')
    assert "business results" in terms.phrases


def test_stop_words_are_dropped_but_never_everything():
    assert analyze("the doc I wrote about AI challenges").tokens == ("ai", "challenges")
    assert analyze("what is this").tokens  # only stop words: kept rather than searching nothing


def test_language_detection():
    assert analyze("Luật đất đai").lang == "vi"
    assert analyze("luat dat dai tranh chap").lang == "vi_nodiac"
    assert analyze("AI challenges report").lang == "en"


def test_like_terms_include_folded_forms():
    assert like_terms(analyze("đất")) == ["đất", "dat"]


def test_matches_text_is_accent_insensitive():
    assert matches_text(analyze("dat dai"), "Luật Đất đai") == 2
