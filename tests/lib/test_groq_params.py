"""Groq must not send `disable_tool_validation` together with a *specified*
tool_choice — the API rejects that combination (400). Capture the request params
by stubbing the client's completions.create and assert the gating.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("groq")
pytest.importorskip("tiktoken")

from app.lib.llm.groq import GroqLLMProvider  # noqa: E402


class _Captured(Exception):
    pass


def _make_provider():
    captured: dict = {}

    class _Completions:
        async def create(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            raise _Captured()  # params recorded; skip response parsing

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    # __new__ bypasses __init__ so we don't build a real Groq client / tiktoken encoder.
    p = GroqLLMProvider.__new__(GroqLLMProvider)
    p.model_name = "openai/gpt-oss-120b"
    p.default_reasoning_effort = None
    p.encoder = None
    p.openai = _Client()
    return p, captured


async def _drain(agen):
    try:
        async for _ in agen:
            pass
    except Exception:
        pass


_TOOLS = [{
    "type": "function",
    "function": {"name": "schedule_create", "parameters": {"type": "object", "properties": {}}},
}]
_MSGS = [{"role": "user", "content": "x"}]


def test_forced_tool_choice_omits_disable_tool_validation():
    p, captured = _make_provider()
    tc = {"type": "function", "function": {"name": "schedule_create"}}
    asyncio.run(_drain(p.chat_completion(messages=_MSGS, tools=_TOOLS, tool_choice=tc)))
    assert captured.get("tool_choice") == tc
    assert "disable_tool_validation" not in captured  # the bug fix


def test_auto_tool_choice_keeps_disable_tool_validation():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(messages=_MSGS, tools=_TOOLS, tool_choice="auto")))
    assert captured.get("tool_choice") == "auto"
    assert captured.get("disable_tool_validation") is True


def test_no_tools_keeps_disable_tool_validation():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(messages=_MSGS)))
    assert "tool_choice" not in captured
    assert captured.get("disable_tool_validation") is True


def test_streaming_path_also_gated():
    p, captured = _make_provider()
    tc = {"type": "function", "function": {"name": "schedule_create"}}
    asyncio.run(_drain(p.chat_completion_stream(messages=_MSGS, tools=_TOOLS, tool_choice=tc)))
    assert captured.get("tool_choice") == tc
    assert "disable_tool_validation" not in captured
