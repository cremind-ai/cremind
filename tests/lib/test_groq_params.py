"""Groq must not send `disable_tool_validation` together with a *specified*
tool_choice — the API rejects that combination (400). Capture the request params
by stubbing the client's completions.create and assert the gating.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("groq")
pytest.importorskip("tiktoken")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
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


def test_stream_omits_stream_options():
    """The Groq SDK's create() has no `stream_options` param — passing it raised
    `unexpected keyword argument 'stream_options'`. Guard against the regression.
    """
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion_stream(messages=_MSGS)))
    assert "stream_options" not in captured


# --- usage capture from Groq's x_groq.usage (cost tracking) ---------------

class _Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _XGroq:
    def __init__(self, usage):
        self.usage = usage


class _Delta:
    content = None
    tool_calls = None


class _Choice:
    def __init__(self, finish_reason=None):
        self.delta = _Delta()
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices=None, usage=None, x_groq=None):
        self.choices = choices or []
        self.usage = usage
        self.x_groq = x_groq


def _make_streaming_provider(chunks):
    """Provider whose create() returns an async iterator over `chunks`."""
    class _Stream:
        def __aiter__(self):
            async def gen():
                for c in chunks:
                    yield c
            return gen()

    class _Completions:
        async def create(self, **kwargs):
            return _Stream()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    p = GroqLLMProvider.__new__(GroqLLMProvider)
    p.model_name = "openai/gpt-oss-120b"
    p.default_reasoning_effort = None
    p.encoder = None
    p.openai = _Client()
    return p


def test_stream_reads_x_groq_usage():
    """Groq surfaces streaming usage under `x_groq.usage`, not top-level `usage`.
    The DONE event must carry non-None token counts so cost tracking works.
    """
    final = _Chunk(
        choices=[_Choice(finish_reason="stop")],
        usage=None,
        x_groq=_XGroq(_Usage(prompt_tokens=123, completion_tokens=45)),
    )
    p = _make_streaming_provider([final])

    async def collect():
        events = []
        async for ev in p.chat_completion_stream(messages=_MSGS):
            events.append(ev)
        return events

    events = asyncio.run(collect())
    done = next(e for e in events if e.get("type") == ChatCompletionTypeEnum.DONE)
    assert done["input_tokens"] == 123
    assert done["output_tokens"] == 45
