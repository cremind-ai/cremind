"""CLI TUI renders per-step reasoning tokens on ``thinking`` events.

Also guards the shared token formatter that backs both the per-turn status line
(`extract_token_usage`) and the new per-step thinking sub-line, and confirms the
``thinking`` branch reads the current backend field names (``Tool`` /
``Tool_Input`` / ``Model_Label`` / ``Token_Usage``) — it previously read stale
``Thought`` / ``Action`` keys the backend no longer emits and rendered nothing.
"""

from __future__ import annotations

import json

from app.cli.client._sse import Event
from app.cli.tui.renderer import (
    _summarize_token_usage,
    default_theme,
    extract_token_usage,
    format_event,
)


def _event(event_type: str, payload: dict) -> Event:
    frame = {"seq": 1, "type": event_type, "data": payload}
    return Event(type=event_type, data=frame, raw=json.dumps(frame))


# ── shared formatter ────────────────────────────────────────────────────────

def test_summarize_token_usage_cache_and_no_cache():
    # ``input_tokens`` is uncached only; cached reads/writes add back for the total.
    assert _summarize_token_usage(
        {"input_tokens": 8, "cache_read_input_tokens": 5, "output_tokens": 2}
    ) == (15, "(in 8, cached 5 / out 2)")
    assert _summarize_token_usage(
        {"input_tokens": 8, "output_tokens": 2}
    ) == (10, "(in 8 / out 2)")
    assert _summarize_token_usage({}) == (0, "")


def test_extract_token_usage_uses_shared_formatter():
    line = extract_token_usage(_event("token_usage", {"token_usage": {
        "input_tokens": 8, "cache_read_input_tokens": 5, "output_tokens": 2,
    }}))
    assert line == "tokens: 15  (in 8, cached 5 / out 2)"


# ── thinking event: tool + per-step tokens ──────────────────────────────────

def test_thinking_event_renders_tool_and_tokens():
    theme = default_theme()
    line = format_event(_event("thinking", {
        "Step": 1, "Call_Id": "c1", "Tool": "documentation_search",
        "Tool_Input": json.dumps({"query": "x"}), "Model_Label": "Fake m",
        "Token_Usage": {
            "input_tokens": 8, "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 0, "output_tokens": 2,
        },
    }), theme)
    assert line is not None and line.kind == "thinking"
    assert "documentation_search" in line.body
    assert "Fake m" in line.body
    assert "tokens (in 8, cached 5 / out 2)" in line.body


def test_thinking_event_without_tokens_still_renders_tool():
    theme = default_theme()
    line = format_event(_event("thinking", {
        "Tool": "exec_shell", "Tool_Input": json.dumps({"command": "ls"}),
    }), theme)
    assert line is not None and line.kind == "thinking"
    assert "exec_shell" in line.body
    assert "tokens" not in line.body  # no Token_Usage → no sub-line


def test_thinking_event_ignores_stale_thought_action_keys():
    # A payload carrying only the retired keys must render nothing, not a blank
    # thinking line — the branch keys off the current ``Tool`` field now.
    theme = default_theme()
    line = format_event(_event("thinking", {
        "Thought": "old", "Action": "old_tool", "Action_Input": {"a": 1},
    }), theme)
    assert line is None
