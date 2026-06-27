"""Unit tests for the ``reasoning`` (think) built-in tool.

The tool is a scratchpad: it takes the model's chain-of-thought and echoes it
straight back as a text result so the reasoning re-enters the model's context
before the next tool call.
"""

from __future__ import annotations

import asyncio


def _run(arguments):
    from app.tools.builtin.reasoning import ReasoningTool
    tool = ReasoningTool()
    return asyncio.run(tool.run(arguments))


def _text(result) -> str:
    assert result.content and result.content[0]["type"] == "text"
    return result.content[0]["text"]


def test_echoes_reasoning_text():
    thought = "User wants X. I have tools A and B. Call A first to confirm the value."
    assert _text(_run({"reasoning": thought})) == thought


def test_strips_surrounding_whitespace():
    assert _text(_run({"reasoning": "  think  "})) == "think"


def test_empty_input_is_handled():
    assert _text(_run({})) == "(no reasoning provided)"
    assert _text(_run({"reasoning": "   "})) == "(no reasoning provided)"


def test_tool_declares_single_reasoning_param():
    from app.tools.builtin.reasoning import ReasoningTool, TOOL_CONFIG
    tool = ReasoningTool()
    assert TOOL_CONFIG["name"] == "reasoning"
    assert TOOL_CONFIG.get("hidden") is True
    assert list(tool.parameters["properties"]) == ["reasoning"]
    assert tool.parameters["required"] == ["reasoning"]
