"""Tests for ReasoningAgent's persisted reasoning-trace assembly.

``_build_llm_messages`` produces the canonical native trace persisted for replay:
``normalize(self._turn_messages) + [final-answer assistant message]``. The pairing
guard (``_normalize_turn_messages``) drops any trailing assistant ``tool_calls``
whose ids are not all answered by a following ``role:"tool"`` result — a dangling
``tool_use`` would make Anthropic/OpenAI 400 on replay.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")  # reasoning_agent imports a2a.types at module load

import app.agent.reasoning_agent as ra  # noqa: E402


def _agent() -> "ra.ReasoningAgent":
    """A ReasoningAgent skeleton — only ``_turn_messages`` is needed here."""
    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent._turn_messages = []
    agent._final_answer_text = ""
    return agent


def _assistant(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }],
    }


def _tool(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def test_build_appends_final_answer() -> None:
    agent = _agent()
    agent._turn_messages = [_assistant("c1"), _tool("c1")]
    out = agent._build_llm_messages("the final answer")
    assert out == [
        _assistant("c1"), _tool("c1"),
        {"role": "assistant", "content": "the final answer"},
    ]


def test_no_tools_returns_none() -> None:
    agent = _agent()
    agent._turn_messages = []
    assert agent._build_llm_messages("hi") is None


def test_balanced_trace_roundtrips() -> None:
    msgs = [_assistant("c1"), _tool("c1")]
    assert ra.ReasoningAgent._normalize_turn_messages(msgs) == msgs


def test_trailing_dangling_tool_call_dropped() -> None:
    agent = _agent()
    # Second assistant has a tool_call with no following result.
    agent._turn_messages = [_assistant("c1"), _tool("c1"), _assistant("c2")]
    out = agent._build_llm_messages("done")
    assert out == [
        _assistant("c1"), _tool("c1"),
        {"role": "assistant", "content": "done"},
    ]


def test_partial_parallel_results_truncate_group() -> None:
    agent = _agent()
    # One assistant with two parallel calls, only the first answered → whole group
    # is unsafe to replay, so the trace collapses to nothing → None (content-only).
    agent._turn_messages = [
        {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            ],
        },
        _tool("c1"),
    ]
    assert agent._build_llm_messages("done") is None
