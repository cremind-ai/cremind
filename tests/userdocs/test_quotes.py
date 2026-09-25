"""Quote verification: exact, normalized, fuzzy — and the changes fuzzy must never absorb.

A fuzzy match exists so that a quote with a dropped word or a different dash
still counts as a quote. It must not become a way for a changed number or a
changed diacritic to pass: in a legal answer "45/2013" vs "45/2014", or
"chứng nhận" (certificate) vs "chứng nhân", is the whole point.
"""

from __future__ import annotations

import pytest

from app.userdocs.quotes import (
    EXACT,
    FUZZY,
    MISMATCH,
    NORMALIZED,
    split_fragments,
    verify_quote,
    worst,
)

SRC = (
    "Điều 203. Thẩm quyền giải quyết tranh chấp đất đai. Tranh chấp đất đai mà "
    "đương sự có Giấy chứng nhận thì do Tòa án nhân dân giải quyết theo Luật số "
    "45/2013/QH13, có hiệu lực từ ngày 01/7/2014."
)
EN = (
    "The supplier shall deliver the goods within 30 days of the order date. "
    "Late delivery incurs a penalty of 2% per week — capped at 10% of the price."
)


def test_exact():
    r = verify_quote("Tranh chấp đất đai mà đương sự có Giấy chứng nhận", [("h1", SRC)])
    assert r.status == EXACT and r.score == 1.0 and r.chunk_hash == "h1"
    assert SRC[r.span[0]:r.span[1]] == r.canonical == "Tranh chấp đất đai mà đương sự có Giấy chứng nhận"


def test_normalized_absorbs_typography_and_case_only():
    # Curly quotes, an en dash for the em dash, different case, a line break.
    quote = "late delivery incurs a penalty of 2% per week – capped\nat 10% of the price"
    r = verify_quote(quote, [EN])
    assert r.status == NORMALIZED
    # The canonical slice is the source's own wording, typography included.
    assert r.canonical == "Late delivery incurs a penalty of 2% per week — capped at 10% of the price"


def test_fuzzy_tolerates_a_dropped_word():
    quote = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án giải quyết"
    r = verify_quote(quote, [SRC])
    assert r.status == FUZZY and r.score >= 0.92
    # Snapped to whole words, and the source's words.
    assert r.canonical.endswith("Tòa án nhân dân giải quyết")


def test_a_substituted_word_is_a_mismatch_however_close_the_characters():
    # "do Tòa án … giải quyết" (the court decides) quoted as "do UBND …"
    # (the People's Committee decides): ~0.93 similar, the opposite authority.
    quote = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do UBND nhân dân giải quyết"
    assert verify_quote(quote, [SRC]).status == MISMATCH
    # A word the source does not have, inside the quote.
    added = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án nhân dân tối cao giải quyết"
    assert verify_quote(added, [SRC]).status == MISMATCH


def test_dropping_a_negation_or_exception_word_is_a_mismatch():
    src = "Người sử dụng đất không được chuyển nhượng quyền sử dụng đất trong thời hạn thuê, trừ trường hợp luật có quy định khác."
    ok = "Người sử dụng đất không được chuyển nhượng quyền sử dụng đất trong thời hạn thuê"
    assert verify_quote(ok, [src]).status == EXACT
    flipped = "Người sử dụng đất được chuyển nhượng quyền sử dụng đất trong thời hạn thuê"
    assert verify_quote(flipped, [src]).status == MISMATCH
    en = "The supplier shall not deliver the goods within 30 days of the order date."
    assert verify_quote("The supplier shall deliver the goods within 30 days of the order date", [en]).status == MISMATCH


def test_a_typo_inside_a_word_is_still_fuzzy():
    quote = "The supplier shall deliver the goods withn 30 days of the order date"
    r = verify_quote(quote, [EN])
    assert r.status == FUZZY
    assert r.canonical == "The supplier shall deliver the goods within 30 days of the order date"


def test_a_changed_number_is_a_mismatch():
    quote = "Tòa án nhân dân giải quyết theo Luật số 45/2014/QH13, có hiệu lực từ ngày 01/7/2014"
    r = verify_quote(quote, [SRC])
    assert r.status == MISMATCH
    assert r.score > 0.95  # close by ratio — rejected on the digits alone
    assert r.canonical is None and r.span is None
    en = verify_quote("Late delivery incurs a penalty of 3% per week — capped at 10% of the price", [EN])
    assert en.status == MISMATCH


def test_a_diacritics_only_difference_is_a_mismatch():
    quote = "Tranh chấp đất đai mà đương sự có Giấy chứng nhân thì do Tòa án nhân dân giải quyết"
    r = verify_quote(quote, [SRC])
    assert r.status == MISMATCH
    # Dropping every accent is no better.
    bare = "Tranh chap dat dai ma duong su co Giay chung nhan thi do Toa an nhan dan giai quyet"
    assert verify_quote(bare, [SRC]).status == MISMATCH


def test_diacritics_are_tolerated_when_the_source_has_none():
    # An unaccented document quoted with a couple of accents restored: the
    # difference is diacritics-only, and that is allowed only because the
    # source has no diacritics to contradict.
    unaccented = "Tranh chap dat dai ma duong su co Giay chung nhan thi do Toa an nhan dan giai quyet theo luat."
    light = "Tranh chap dat dai ma duong su co Giay chứng nhận thi do Toa an nhan dan giai quyet"
    assert verify_quote(light, [unaccented]).status == FUZZY
    # The same edit against an accented source is rejected.
    accented = unaccented.replace("chung nhan", "chứng nhân")
    assert verify_quote(light, [accented]).status == MISMATCH


def test_ellipses_split_into_ordered_fragments():
    r = verify_quote("Tranh chấp đất đai … Tòa án nhân dân giải quyết", [SRC])
    assert r.status == EXACT
    assert r.canonical == "Tranh chấp đất đai … Tòa án nhân dân giải quyết"
    # Out of order is not a quote.
    assert verify_quote("Tòa án nhân dân giải quyết … Thẩm quyền giải quyết", [SRC]).status == MISMATCH
    # Leading/trailing ellipses only say "mid-sentence".
    assert verify_quote("…Thẩm quyền giải quyết tranh chấp đất đai...", [SRC]).status == EXACT
    assert split_fragments("“…a … b [...] c (…) d…”") == ["a", "b", "c", "d"]


def test_the_worst_fragment_decides():
    r = verify_quote("Tranh chấp đất đai … Tòa án nhân dân giải quyết theo Luật số 45/2019/QH13", [SRC])
    assert r.status == MISMATCH


def test_candidates_are_tried_in_turn():
    chunk = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận"
    joined = "Điều 203. Thẩm quyền giải quyết tranh chấp đất đai.\n" + chunk
    quote = "Thẩm quyền giải quyết tranh chấp đất đai. Tranh chấp đất đai mà đương sự"
    r = verify_quote(quote, [("c", chunk), ("c", joined)])
    assert r.status in (EXACT, NORMALIZED)
    assert r.chunk_hash == "c"


def test_decomposed_text_is_compared_as_composed():
    import unicodedata

    nfd = unicodedata.normalize("NFD", SRC)
    r = verify_quote("Tòa án nhân dân giải quyết", [nfd])
    assert r.status == EXACT
    assert r.canonical == "Tòa án nhân dân giải quyết"


def test_short_quotes_are_never_fuzzy():
    assert verify_quote("Tòa án nhân", [SRC]).status == EXACT
    assert verify_quote("Tòa án nhan", [SRC]).status == MISMATCH


@pytest.mark.parametrize("quote", ["", "   ", "…", "“”"])
def test_empty_quotes_are_mismatches(quote):
    assert verify_quote(quote, [SRC]).status == MISMATCH


def test_worst():
    assert worst([EXACT, FUZZY, NORMALIZED]) == FUZZY
    assert worst([EXACT, MISMATCH]) == MISMATCH
    assert worst([]) is None
