"""Delegated sub-agents (claude_code/codex) don't contribute to turn usage.

Those tools report no ``token_usage`` on their result, so the adapter folds zero and
the reasoning agent records nothing for them — their cost/tokens are surfaced only in
the Agent Activity panel. Ordinary internal-LLM tools (documentation_search, image
understanding) still fold their usage into a ``source_kind="tool"`` record with the
parent model.
"""

from __future__ import annotations

import types

from app.agent.reasoning_agent import ReasoningAgent
from app.tools import ToolType


class _FakeAgent:
    """Minimal host binding the real recorder method (no full agent needed)."""

    _SOURCE_KIND_BY_TOOL_TYPE = ReasoningAgent._SOURCE_KIND_BY_TOOL_TYPE
    _provider_model_for = ReasoningAgent._provider_model_for
    _record_tool_usage = ReasoningAgent._record_tool_usage

    def __init__(self, llm=None):
        self._usage_records = []
        self.current_step_count = 3
        self.llm = llm


def _tool(tool_type, tool_id, name, inner=None):
    adapter = types.SimpleNamespace(_llm=inner) if inner is not None else None
    return types.SimpleNamespace(
        tool_type=tool_type, tool_id=tool_id, name=name, adapter=adapter,
    )


def test_zero_usage_records_nothing():
    """A delegated sub-agent reports no usage (all-zero token_usage) ⇒ no record,
    so it contributes nothing to the turn's token count / cost."""
    agent = _FakeAgent()
    tool = _tool(ToolType.BUILTIN, "claude_code", "Claude Code")
    agent._record_tool_usage(tool, {
        "input_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0,
    })
    assert agent._usage_records == []


def test_ordinary_tool_usage_is_recorded():
    """An internal-LLM tool with real tokens still folds into a 'tool' record with
    the parent model — only claude_code/codex are excluded."""
    agent = _FakeAgent()
    inner = types.SimpleNamespace(provider_name="github-copilot", model_name="gpt-4.1")
    tool = _tool(ToolType.BUILTIN, "documentation_search", "documentation_search", inner=inner)
    agent._record_tool_usage(tool, {
        "input_tokens": 200, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 6,
    })
    rec = agent._usage_records[0]
    assert rec.source_kind == "tool"
    assert rec.provider == "github-copilot"
    assert rec.model == "gpt-4.1"
