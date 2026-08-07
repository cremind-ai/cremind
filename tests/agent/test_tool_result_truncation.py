"""A clipped tool result must SAY it was clipped.

``tool_result.max_tokens`` clamps every tool result before it reaches the model.
Clipping silently (the original behaviour) makes a successful command look like
it produced broken output, and models respond by re-running it — redirecting to
a file, re-sorting, reading the tool's source — chasing a tail that the runtime,
not the command, removed. The marker tells the model the command succeeded and
what to do instead (narrow the output).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _agent(*, enabled: bool = True, max_tokens: int = 50) -> ra.ReasoningAgent:
    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent._turn_messages = []
    agent._tool_result_enabled = enabled
    agent._tool_result_max_tokens = max_tokens
    return agent


def _content(agent: ra.ReasoningAgent) -> str:
    assert len(agent._turn_messages) == 1
    msg = agent._turn_messages[0]
    assert msg["role"] == "tool"
    return msg["content"]


def test_oversized_result_is_marked_truncated():
    agent = _agent(max_tokens=50)
    agent._append_tool_result("call_1", "word " * 500)

    content = _content(agent)
    assert "truncated" in content
    # The two failure modes the marker exists to prevent.
    assert "SUCCEEDED" in content
    assert "--max-results" in content
    # The head of the real output survives ahead of the marker.
    assert content.startswith("word word")


def test_short_result_is_untouched():
    agent = _agent(max_tokens=50)
    agent._append_tool_result("call_1", "all good")

    assert _content(agent) == "all good"


def test_clamp_disabled_never_marks():
    agent = _agent(enabled=False, max_tokens=50)
    agent._append_tool_result("call_1", "word " * 500)

    content = _content(agent)
    assert "truncated" not in content
    assert len(content) == len("word " * 500)


def test_truncate_false_bypasses_the_clamp():
    """Skill loads pass ``truncate=False`` so the full SKILL.md reaches context."""
    agent = _agent(max_tokens=50)
    agent._append_tool_result("call_1", "word " * 500, truncate=False)

    assert "truncated" not in _content(agent)


def test_empty_result_is_placeholdered_not_marked():
    agent = _agent(max_tokens=50)
    agent._append_tool_result("call_1", "")

    assert _content(agent) == "No result"
