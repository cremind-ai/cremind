"""What a seated agent is told about the room it is sitting in.

Two things are pinned here, and both were bugs before they were tests.

The routing note is written by :mod:`app.groups.render` and explained by the
prompt, so the two have to agree on the literal string — the day they drift, the
agents are watching for something nothing sends and the annotation becomes
noise they were never taught to read.

And the SPEAK rule has to cover a greeting. "Hello everyone" was routed to the
whole room, woke every agent, and got no reply from one of them: the rule said
"when a person ASKS something meant for everyone", a greeting asks nothing, and
the model declined on a technicality.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from app.agent.reasoning_agent import _format_group_chat_block  # noqa: E402
from app.groups.render import (  # noqa: E402
    ROUTING_NOTE_EVERYONE,
    ROUTING_NOTE_YOU,
    routing_note_for_names,
)


def _origin(**overrides):
    origin = {
        "group_name": "Household",
        "self_profile": "dog",
        "self_name": "Rex",
        "members": [
            {"profile": "dog", "agent_name": "Rex"},
            {"profile": "cat", "agent_name": "Mimi"},
        ],
    }
    origin.update(overrides)
    return origin


def test_no_room_no_block() -> None:
    assert _format_group_chat_block(None) == ""
    assert _format_group_chat_block({}) == ""


def test_the_block_names_the_room_the_agent_and_who_else_is_in_it() -> None:
    block = _format_group_chat_block(_origin())
    assert 'your seat in the group "Household"' in block
    assert "You are Rex" in block
    assert "Mimi" in block
    # The agent has to be able to find itself in the roster.
    assert "Rex — you" in block


def test_the_block_teaches_exactly_the_notes_the_renderer_writes() -> None:
    """The drift guard: the strings come from the module that produces them."""
    block = _format_group_chat_block(_origin())
    assert ROUTING_NOTE_YOU in block
    assert ROUTING_NOTE_EVERYONE in block
    # And it is told not to write one itself — an echoed note would otherwise be
    # posted to the room, or (stuck to the sentinel) defeat the silence.
    assert "Never write a note like that yourself" in block


def test_a_greeting_to_the_room_is_covered_by_the_speak_rule() -> None:
    """The exact failure from the report: routed to everyone, woken, silent."""
    block = _format_group_chat_block(_origin())
    assert "greets or asks the whole room" in block
    assert "hello everyone" in block


def test_silence_is_still_licensed_and_still_has_to_be_the_whole_answer() -> None:
    """Broadening SPEAK must not cost the sentinel its teeth: most turns in a
    busy room should still produce nothing at all."""
    block = _format_group_chat_block(_origin())
    assert "STAY SILENT otherwise" in block
    assert "your ENTIRE answer must be exactly: [silent]" in block


def test_the_note_helper_and_the_prompt_agree_on_the_shape() -> None:
    """A note naming others is described but not spelled out in the prompt, so
    this is what pins the shape they share."""
    assert routing_note_for_names(["you"]) == ROUTING_NOTE_YOU
    assert routing_note_for_names(["Mimi"]).startswith("[to: ")
    assert "A note naming other members" in _format_group_chat_block(_origin())


def test_an_automation_result_is_licensed_to_speak() -> None:
    """A digest the room asked for must not be answered [silent].

    An automation's result re-enters the seat carrying none of the room's cues —
    no sender prefix, no routing note, nobody addressing anyone — so under the
    SPEAK / STAY SILENT rules alone it reads as somebody else's traffic, and the
    thing the room set up is exactly what never gets posted.
    """
    block = _format_group_chat_block(_origin())
    assert "YOUR OWN automation reporting back" in block
    assert "[Event result]" in block
    assert "nothing new to report" in block
    # After the interruption rule and before style: it belongs with the other
    # "when does this count as speech" clauses.
    assert block.index("INTERRUPTIONS:") < block.index("AUTOMATION RESULTS:")
    assert block.index("AUTOMATION RESULTS:") < block.index("STYLE:")


def test_both_rooms_share_one_automation_clause() -> None:
    """A Cremind room and a platform group must not drift apart on this."""
    from app.agent.reasoning_agent import (
        _ROOM_EVENT_RESULT_CLAUSE,
        _format_channel_group_block,
    )

    assert _ROOM_EVENT_RESULT_CLAUSE in _format_group_chat_block(_origin())
    channel_block = _format_channel_group_block({
        "group_name": "Ops",
        "self_name": "Rex",
        "members": [],
    })
    assert _ROOM_EVENT_RESULT_CLAUSE in channel_block
