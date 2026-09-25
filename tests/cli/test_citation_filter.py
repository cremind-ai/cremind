"""The chat TUI's citation filter: "[1]" markers while the answer streams.

The server streams the answer in arbitrary pieces, so a token routinely
arrives split ("…see [ud:k7m2" + "xq9a#3f9c2e1b]"). The filter holds back a
tail that could still become a token — never more than 32 characters after
the bracket, so a stray "[" does not stall the text — rewrites whole tokens to
the same numbers the server and web UI use, and prints the "Sources:" footer
when the turn's ``citations`` event arrives.
"""

from __future__ import annotations

import re

from app.cli.client._sse import Event
from app.cli.tui.renderer import CitationInlineFilter, format_event, monochrome_theme
from app.userdocs.cite import number_tokens

T1 = "[ud:k7m2xq9a#3f9c2e1b]"
T2 = "[ud:p4n8wr2c]"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _ev(etype, data):
    return Event(type=etype, data={"type": etype, "data": data}, raw="")


def _feed_all(f: CitationInlineFilter, pieces) -> str:
    return "".join(f.feed(p) for p in pieces) + f.flush()


def test_a_token_split_across_chunks_is_held_and_rewritten():
    f = CitationInlineFilter()
    assert f.feed("Courts decide [ud:k7m2") == "Courts decide "
    assert f.feed("xq9a#3f9c") == ""
    assert f.feed("2e1b]. Next") == "[1]. Next"


def test_every_split_point_gives_the_same_text():
    answer = f"Courts decide {T1}, mediation first {T2}; again {T1}."
    expected = "Courts decide [1], mediation first [2]; again [1]."
    for cut in range(1, len(answer)):
        f = CitationInlineFilter()
        assert _feed_all(f, [answer[:cut], answer[cut:]]) == expected, cut
    # One character at a time, too.
    f = CitationInlineFilter()
    assert _feed_all(f, list(answer)) == expected


def test_numbering_matches_the_server():
    answer = f"a {T2} b {T1} c {T2}"
    numbers = number_tokens(answer)
    f = CitationInlineFilter()
    out = _feed_all(f, [answer])
    assert out == f"a [{numbers[T2]}] b [{numbers[T1]}] c [{numbers[T2]}]"


def test_ordinary_brackets_are_not_held():
    f = CitationInlineFilter()
    assert f.feed("see [1] and [note") == "see [1] and [note"
    assert f.feed("a list [") == "a list "  # could still be a token…
    assert f.feed("x]") == "[x]"            # …and was not
    # Never more than 32 characters are held after a bracket.
    f = CitationInlineFilter()
    out = f.feed("[ud:" + "a" * 40)
    assert out == "[ud:" + "a" * 40


def test_tolerant_forms_are_rewritten():
    f = CitationInlineFilter()
    out = _feed_all(f, ["Both 【ud: K7M2XQ9A#3F9C", "2E1B】 and [ud:k7m2xq9a#3f9c2e1b; ud:p4n8wr2c]"])
    assert out == "Both [1] and [1][2]"


def test_format_event_streams_then_prints_the_sources_footer():
    theme = monochrome_theme()
    f = CitationInlineFilter()
    lines = [
        format_event(_ev("text", {"token": "Courts decide [ud:k7m2"}), theme, f),
        format_event(_ev("text", {"token": "xq9a#3f9c2e1b] and "}), theme, f),
        format_event(_ev("text", {"token": f"{T2}."}), theme, f),
        format_event(_ev("citations", {"assistant_id": "m1", "citations": {"v": 1, "unverified": 1, "items": [
            {"n": 1, "token": T1, "status": "verified", "quote_status": None,
             "file": {"fid": "k7m2xq9a", "name": "luat.pdf", "rel_path": "Luat/luat.pdf", "source": "local",
                      "kind": "pdf", "web_link": None},
             "locator": {"page": 3}, "locator_label": "p. 3", "snippet": ""},
            {"n": 2, "token": T2, "status": "invalid", "quote_status": None, "file": None,
             "locator": {}, "locator_label": "", "snippet": ""},
        ]}}), theme, f),
        format_event(_ev("complete", {"assistant_id": "m1"}), theme, f),
    ]
    bodies = [_ANSI.sub("", ln.body) if ln else None for ln in lines]
    assert bodies[0] == "Courts decide "
    assert bodies[1] == "[1] and "
    assert bodies[2] == "[2]."
    assert lines[3].kind == "text"  # appended to the answer it belongs to
    assert bodies[3] == "\n\nSources:\n[1] luat.pdf · p. 3 · Luat\n[2] unknown source (not verified)"
    assert bodies[4].startswith("* run complete")
    # The next turn numbers from 1 again.
    assert _ANSI.sub("", format_event(_ev("text", {"token": f"{T2} "}), theme, f).body) == "[1] "


def test_a_turn_ending_mid_bracket_still_shows_the_text():
    theme = monochrome_theme()
    f = CitationInlineFilter()
    assert format_event(_ev("text", {"token": "odd ending ["}), theme, f).body == "odd ending "
    line = format_event(_ev("complete", {}), theme, f)
    assert _ANSI.sub("", line.body).startswith("[\n* run complete")


def test_passing_none_switches_rewriting_off():
    theme = monochrome_theme()
    line = format_event(_ev("text", {"token": f"x {T1}"}), theme, None)
    assert _ANSI.sub("", line.body) == f"x {T1}"
    assert format_event(_ev("citations", {"citations": {"items": []}}), theme, None) is None
