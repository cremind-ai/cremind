"""CLI TUI reports the turn clock: elapsed per step, and the turn's totals.

The runner stamps every ``thinking`` frame with ``Elapsed_Ms`` and puts the
turn's milestones on ``complete``. A terminal watching a run sees the same
numbers a bubble does, so "why did that take so long" is answerable from either.

Frames from a run that predates the stamps carry neither, and must render
exactly as they did before rather than printing a placeholder.
"""

from __future__ import annotations

import json

from app.cli.client._sse import Event
from app.cli.tui.renderer import default_theme, format_elapsed_ms, format_event


def _event(event_type: str, payload: dict) -> Event:
    frame = {"seq": 1, "type": event_type, "data": payload}
    return Event(type=event_type, data=frame, raw=json.dumps(frame))


# ── the formatter ───────────────────────────────────────────────────────────

def test_format_elapsed_ms_switches_unit_at_a_second():
    assert format_elapsed_ms(0) == "0ms"
    assert format_elapsed_ms(999) == "999ms"
    assert format_elapsed_ms(1000) == "1.0s"
    assert format_elapsed_ms(4823) == "4.8s"


def test_format_elapsed_ms_is_silent_on_nothing_usable():
    # ``first_step_ms`` is None for a turn that called no tool, and old frames
    # carry no stamp at all — neither is an error, both render as nothing.
    assert format_elapsed_ms(None) == ""
    assert format_elapsed_ms("soon") == ""
    assert format_elapsed_ms(-1) == ""


# ── thinking: where the step fell in the turn ───────────────────────────────

def test_thinking_event_shows_elapsed():
    theme = default_theme()
    line = format_event(_event("thinking", {
        "Tool": "exec_shell", "Tool_Input": json.dumps({"command": "ls"}),
        "Elapsed_Ms": 1240,
    }), theme)
    assert line is not None
    assert "+1.2s" in line.body


def test_thinking_event_without_a_stamp_is_unchanged():
    theme = default_theme()
    line = format_event(_event("thinking", {
        "Tool": "exec_shell", "Tool_Input": json.dumps({"command": "ls"}),
    }), theme)
    assert line is not None
    assert "+" not in line.body


# ── complete: the turn's milestones ─────────────────────────────────────────

def test_complete_reports_first_token_and_total():
    theme = default_theme()
    line = format_event(_event("complete", {
        "assistant_id": "m1",
        "latency": {"first_step_ms": 900, "first_token_ms": 4823, "total_ms": 5617},
    }), theme)
    assert line is not None
    assert "first token 4.8s" in line.body
    assert "total 5.6s" in line.body


def test_complete_without_latency_stays_a_bare_line():
    theme = default_theme()
    line = format_event(_event("complete", {"assistant_id": "m1"}), theme)
    assert line is not None
    assert "* run complete" in line.body
    assert "(" not in line.body


def test_complete_drops_a_milestone_the_turn_never_reached():
    # A turn that errored before the model spoke has no first token; the line
    # says what it knows instead of printing an empty value.
    theme = default_theme()
    line = format_event(_event("complete", {
        "latency": {"first_step_ms": None, "first_token_ms": None, "total_ms": 320},
    }), theme)
    assert line is not None
    assert "first token" not in line.body
    assert "total 320ms" in line.body
