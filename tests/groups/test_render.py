"""Turning a group post into text a model reads, and a finished turn back out.

The two directions fail in opposite ways, which is why they are tested with
opposite biases:

``is_silent`` is deliberately tolerant, because a missed sentinel is a literal
``[silent]`` posted into a room and mirrored to Telegram, while a false positive
costs one unsent line. What it must NOT swallow is a message that merely starts
with the sentinel — the agent had something to add.

``split_segments`` is where the sentinel is actually judged. A turn interrupted
mid-flight speaks twice ("Got it, checking" then ``[silent]``), and testing the
concatenation as one string finds no sentinel and posts the whole thing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from app.groups.constants import SILENT_SENTINEL  # noqa: E402
from app.groups.render import (  # noqa: E402
    ROUTING_NOTE_EVERYONE,
    ROUTING_NOTE_YOU,
    is_silent,
    is_silent_ignoring_notes,
    render_attributed,
    routing_note_for_names,
    split_segments,
    strip_routing_notes,
)


# ── attribution ─────────────────────────────────────────────────────────────


def test_render_attributed_names_the_speaker_and_what_they_are() -> None:
    """The kind is spelled out because it is the basis of the agent's whole
    judgement: a user assigns work, a peer agent coordinates."""
    assert render_attributed("Alexa", "user", "what time is it?") == (
        "Alexa (user): what time is it?"
    )
    assert render_attributed("Rex", "agent", "on it") == "Rex (agent): on it"
    assert render_attributed("Cremind", "system", "Mia failed.") == (
        "Cremind (system): Mia failed."
    )


def test_render_attributed_never_produces_an_anonymous_line() -> None:
    """An unattributed line in a room of several speakers is worse than an
    awkwardly-named one."""
    assert render_attributed("", "user", "hi") == "Someone (user): hi"
    assert render_attributed("  ", "", "hi") == "Someone (user): hi"
    assert render_attributed("  Alexa  ", "user", "hi") == "Alexa (user): hi"


# ── the silence sentinel ────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    SILENT_SENTINEL,
    "[silent]",
    "**[silent]**",
    "[silent].",
    " [SILENT] ",
    "[Silent]",
    "`[silent]`",
    '"[silent]"',
    "silent",
    "[silent]\n",
])
def test_a_dressed_up_sentinel_still_means_silence(text: str) -> None:
    """Models wrap sentinels in bold, quotes and full stops without meaning to
    change them."""
    assert is_silent(text) is True


@pytest.mark.parametrize("text", ["", "   ", "\n\n", None])
def test_nothing_at_all_is_silence(text) -> None:
    """A turn that produced no text has nothing to post."""
    assert is_silent(text) is True


@pytest.mark.parametrize("text", [
    "[silent] but the server is down",
    "I will stay [silent] on this one",
    "It is 14:20.",
    "silently checking",
    "not silent",
])
def test_text_that_says_more_than_the_sentinel_is_not_silence(text: str) -> None:
    """The failure this guards is the opposite of a missed sentinel: an agent
    that had something to say being muted."""
    assert is_silent(text) is False


# ── splitting a turn back into what was said ────────────────────────────────


def test_no_breaks_is_one_segment() -> None:
    assert split_segments("It is 14:20.", []) == ["It is 14:20."]
    assert split_segments("It is 14:20.", None) == ["It is 14:20."]


def test_one_break_splits_the_ack_from_the_answer() -> None:
    text = "Got it, checking.\n\nIt is 14:20."
    assert split_segments(text, [{"content_offset": 17}]) == [
        "Got it, checking.", "It is 14:20.",
    ]


def test_two_breaks_produce_three_pieces_in_order() -> None:
    text = "one\n\ntwo\n\nthree"
    assert split_segments(text, [
        {"content_offset": 8}, {"content_offset": 3},
    ]) == ["one", "two", "three"]


def test_an_offset_of_zero_cuts_nothing() -> None:
    """An interruption arriving before the agent had said anything."""
    assert split_segments("hello", [{"content_offset": 0}]) == ["hello"]
    assert split_segments("hello", [{"content_offset": None}]) == ["hello"]


def test_an_out_of_range_offset_is_ignored() -> None:
    """The offsets are recorded against the live stream; a truncated or
    re-written final text must not produce an empty tail segment."""
    assert split_segments("hello", [{"content_offset": 5}]) == ["hello"]
    assert split_segments("hello", [{"content_offset": 99}]) == ["hello"]
    assert split_segments("hello", [{"content_offset": -3}]) == ["hello"]


def test_an_empty_middle_piece_is_dropped() -> None:
    """Two breaks in a row (an ack the agent decided to SKIP) leave whitespace
    between them, which is not a post."""
    text = "one\n\n   \n\ntwo"
    out = split_segments(text, [{"content_offset": 5}, {"content_offset": 10}])
    assert out == ["one", "two"]


def test_non_dict_breaks_are_skipped_rather_than_raising() -> None:
    assert split_segments("hello there", ["nonsense", {"content_offset": 5}]) == [
        "hello", "there",
    ]


def test_an_empty_turn_produces_no_segments() -> None:
    assert split_segments("", [{"content_offset": 2}]) == []
    assert split_segments(None, None) == []
    assert split_segments("   ", None) == []


def test_the_ack_survives_when_the_final_is_the_sentinel() -> None:
    """The case the split exists for: judged as one string this posts nothing
    (or worse, posts the sentinel); judged per segment the ack stands."""
    text = "Got it, checking.\n\n[silent]"
    segments = split_segments(text, [{"content_offset": 17}])
    assert [s for s in segments if not is_silent(s)] == ["Got it, checking."]


# ── routing notes ───────────────────────────────────────────────────────────


def test_the_note_lists_the_reader_first() -> None:
    """A bare "[to: you]" on a two-target decision reads as "only you", which
    would have each woken agent answer as though it were alone."""
    assert routing_note_for_names(["you"]) == ROUTING_NOTE_YOU
    assert routing_note_for_names(["you", "Mimi"]) == "[to: you, Mimi]"
    assert routing_note_for_names(["Mimi", "Rex"]) == "[to: Mimi, Rex]"
    # Nothing to name is no note at all, never an empty pair of brackets.
    assert routing_note_for_names([]) == ""
    assert routing_note_for_names(["", "  "]) == ""


def test_an_echoed_note_does_not_defeat_the_sentinel() -> None:
    """The whole reason the two live in one module. Every line a seat receives
    now ends with a note, so an agent copying the shape onto its own answer is a
    matter of time — and with one stuck to it the sentinel no longer reduces to
    "silent", so the turn that meant to say nothing would post the word
    "[silent]" to the room and out to every bound platform."""
    assert is_silent(f"{SILENT_SENTINEL}\n{ROUTING_NOTE_YOU}") is False  # the hazard
    assert is_silent(strip_routing_notes(f"{SILENT_SENTINEL}\n{ROUTING_NOTE_YOU}"))
    assert is_silent(strip_routing_notes(f"[silent]\n{ROUTING_NOTE_EVERYONE}"))
    assert is_silent(strip_routing_notes("[silent]\n[to: Mimi, Rex]"))


def test_stripping_leaves_a_real_answer_alone() -> None:
    assert strip_routing_notes("It is 14:20.") == "It is 14:20."
    assert strip_routing_notes(f"It is 14:20.\n{ROUTING_NOTE_YOU}") == "It is 14:20."
    # Trailing notes only: one quoted mid-answer is the agent talking ABOUT a
    # note, and rewriting what an agent said is worse than leaving it.
    quoted = "You wrote [to: you] at the end — that is added for you."
    assert strip_routing_notes(quoted) == quoted
    assert strip_routing_notes("") == ""
    assert strip_routing_notes(None) == ""


def test_several_stacked_notes_all_go() -> None:
    assert strip_routing_notes(
        f"done\n{ROUTING_NOTE_YOU}\n{ROUTING_NOTE_EVERYONE}\n"
    ) == "done"


@pytest.mark.parametrize("text", [
    f"{SILENT_SENTINEL}\n{ROUTING_NOTE_YOU}",
    f"{SILENT_SENTINEL} {ROUTING_NOTE_YOU}",          # same line — the gap
    f"{SILENT_SENTINEL}\n\n{ROUTING_NOTE_EVERYONE}",
    f"**[silent]** {ROUTING_NOTE_EVERYONE}",
    "[silent]  [to: Mimi, Rex]  ",
    ROUTING_NOTE_YOU,                                  # the note and nothing else
])
def test_no_arrangement_of_an_echoed_note_gets_the_sentinel_posted(text: str) -> None:
    """The judgement may be as tolerant as it likes — nothing is published
    either way — so it takes the note off the last LINE as well as off its own
    line. Whole-line stripping alone leaves "[silent] [to: you]" looking like an
    answer, and the room is shown the word the sentinel exists to hide."""
    assert is_silent_ignoring_notes(text) is True


@pytest.mark.parametrize("text", [
    "It is 14:20.",
    f"It is 14:20. {ROUTING_NOTE_YOU}",
    "Forward it [to: Mimi]",
    f"Got it, checking.\n{ROUTING_NOTE_EVERYONE}\nIt is 14:20.",
])
def test_an_answer_is_never_judged_silent_because_of_a_note(text: str) -> None:
    assert is_silent_ignoring_notes(text) is False


def test_judging_and_publishing_are_deliberately_different() -> None:
    """The looser test may not be used to rewrite a post: an answer ending in a
    bracketed phrase is a sentence, and editing it silently is worse than
    leaving the note in."""
    assert strip_routing_notes("Forward it [to: Mimi]") == "Forward it [to: Mimi]"
    assert is_silent_ignoring_notes("Forward it [to: Mimi]") is False
    # And the strict test is still the one that must NOT be used on its own.
    assert is_silent(f"{SILENT_SENTINEL} {ROUTING_NOTE_YOU}") is False
