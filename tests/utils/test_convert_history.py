"""Tests for ``convert_db_messages_to_history`` reasoning-trace replay.

Pins the read-path behavior the prompt cache depends on:

- with ``include_reasoning`` on, a turn's stored ``llm_messages`` trace is spliced
  into history VERBATIM (byte-for-byte, including tool-call ``arguments``) in place
  of the content-only message — so the replayed prefix matches what the model saw;
- with it off, only the final-answer ``content`` is sent (the legacy behavior);
- old (no-trace) and new (trace) turns mix into one valid sequence;
- empty-content messages with no trace are still skipped.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")  # app.utils.common imports a2a.types at module load

from app.utils.common import convert_db_messages_to_history  # noqa: E402


def _trace() -> list[dict]:
    """A realistic one-step trace: assistant tool_call, tool result, final answer."""
    return [
        {
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [{
                "id": "call_0",
                "type": "function",
                "function": {"name": "weather", "arguments": '{"city": "Hanoi"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "Sunny, 31C"},
        {"role": "assistant", "content": "It's sunny, 31C."},
    ]


def test_splice_replaces_content_with_trace_verbatim() -> None:
    trace = _trace()
    db = [
        {"role": "user", "content": "weather in Hanoi?"},
        {"role": "agent", "content": "It's sunny, 31C.", "llm_messages": trace},
    ]
    out = convert_db_messages_to_history(db, include_reasoning=True)

    assert out[0] == {"role": "user", "content": "weather in Hanoi?"}
    # Trace spliced verbatim — same objects, same order.
    assert out[1:] == trace
    # The final answer is present exactly once (no duplication with content).
    assert sum(1 for m in out if m.get("content") == "It's sunny, 31C.") == 1
    # tool-call arguments preserved byte-for-byte (byte-stability → cache hit).
    assert out[1]["tool_calls"][0]["function"]["arguments"] == '{"city": "Hanoi"}'


def test_disabled_uses_content_only() -> None:
    db = [
        {"role": "user", "content": "weather in Hanoi?"},
        {"role": "agent", "content": "It's sunny, 31C.", "llm_messages": _trace()},
    ]
    out = convert_db_messages_to_history(db, include_reasoning=False)
    assert out == [
        {"role": "user", "content": "weather in Hanoi?"},
        {"role": "assistant", "content": "It's sunny, 31C."},
    ]


def test_default_is_content_only() -> None:
    # No keyword → legacy behavior (callers must opt in).
    db = [{"role": "agent", "content": "hi", "llm_messages": _trace()}]
    assert convert_db_messages_to_history(db) == [{"role": "assistant", "content": "hi"}]


def test_mixed_old_and_new_turns() -> None:
    trace = _trace()
    db = [
        {"role": "user", "content": "q1"},
        {"role": "agent", "content": "a1"},                       # old: no trace
        {"role": "user", "content": "q2"},
        {"role": "agent", "content": "It's sunny, 31C.", "llm_messages": trace},  # new
    ]
    out = convert_db_messages_to_history(db, include_reasoning=True)
    assert out[0] == {"role": "user", "content": "q1"}
    assert out[1] == {"role": "assistant", "content": "a1"}       # content-only fallback
    assert out[2] == {"role": "user", "content": "q2"}
    assert out[3:] == trace                                       # spliced trace


def test_empty_content_without_trace_skipped() -> None:
    db = [{"role": "agent", "content": "   ", "llm_messages": None}]
    assert convert_db_messages_to_history(db, include_reasoning=True) == []


def test_ui_only_messages_excluded_from_agent_history() -> None:
    # Rejected skill-event triggers carry metadata.ui_only=True. They are shown
    # in the UI but must never enter the model's context — on every history path.
    db = [
        {"role": "user", "content": "q1"},
        {
            "role": "agent",
            "content": "Trigger: new_email\nAction: ...\nContent: ...",
            "metadata": {"ui_only": True, "kind": "rejected_trigger"},
        },
        {"role": "agent", "content": "a1"},
    ]
    out = convert_db_messages_to_history(db, include_reasoning=False)
    assert out == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]


def test_ui_only_excluded_even_with_reasoning_trace() -> None:
    # A ui_only flag wins even if the row somehow carries an llm_messages trace.
    db = [
        {
            "role": "agent",
            "content": "rejected",
            "metadata": {"ui_only": True},
            "llm_messages": _trace(),
        },
    ]
    assert convert_db_messages_to_history(db, include_reasoning=True) == []


def test_metadata_none_or_missing_is_safe() -> None:
    # metadata may be absent or explicitly None — neither should raise.
    db = [
        {"role": "user", "content": "q1"},
        {"role": "agent", "content": "a1", "metadata": None},
    ]
    out = convert_db_messages_to_history(db)
    assert out == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]


# ── mid-turn user messages ──
#
# A message sent while a turn was running is persisted immediately, then either
# injected into that turn or run as its own. Its ``mid_turn.state`` decides which
# copy the model sees, so that it sees exactly one.


def _mid_turn(state: str, content: str = "and use staging") -> dict:
    return {
        "role": "user",
        "content": content,
        "metadata": {"mid_turn": {"state": state, "run_id": "msg:c:1"}},
    }


@pytest.mark.parametrize("include_reasoning", [True, False])
def test_pending_mid_turn_row_is_always_excluded(include_reasoning: bool) -> None:
    """It is parked right now: the turn about to speak for it will also be given
    it by injection, so including it here would feed the same words twice."""
    db = [{"role": "user", "content": "q1"}, _mid_turn("pending")]
    out = convert_db_messages_to_history(db, include_reasoning=include_reasoning)
    assert out == [{"role": "user", "content": "q1"}]


def test_consumed_mid_turn_row_defers_to_the_trace() -> None:
    """The turn that absorbed it replays it verbatim inside llm_messages."""
    db = [
        _mid_turn("consumed"),
        {"role": "agent", "content": "done", "llm_messages": _trace()},
    ]
    out = convert_db_messages_to_history(db, include_reasoning=True)
    assert out == _trace()


def test_consumed_mid_turn_row_is_included_when_replay_is_off() -> None:
    """With no trace replay nothing else carries the text, so the row must."""
    db = [
        _mid_turn("consumed"),
        {"role": "agent", "content": "done", "llm_messages": _trace()},
    ]
    out = convert_db_messages_to_history(db, include_reasoning=False)
    assert out == [
        {"role": "user", "content": "and use staging"},
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.parametrize("include_reasoning", [True, False])
def test_released_mid_turn_row_is_an_ordinary_user_message(
    include_reasoning: bool,
) -> None:
    """It was never injected (or the turn died first), so it runs as its own."""
    db = [_mid_turn("released")]
    out = convert_db_messages_to_history(db, include_reasoning=include_reasoning)
    assert out == [{"role": "user", "content": "and use staging"}]


def test_an_unknown_mid_turn_state_is_not_silently_hidden() -> None:
    """Only the three known states filter; anything else stays visible rather
    than vanishing from the model's context."""
    db = [_mid_turn("something-new")]
    assert convert_db_messages_to_history(db) == [
        {"role": "user", "content": "and use staging"},
    ]
