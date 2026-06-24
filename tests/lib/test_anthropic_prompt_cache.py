"""Anthropic prompt-cache markers must be placed on the right blocks and the
cacheable prefix must render byte-identically across reasoning steps.

Caching fails *silently* (a stray invalidator just yields ``cache_read=0``,
never an error), so these tests pin the marker placement and prefix stability.
Params are captured by stubbing ``client.messages.create``.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("openai")

from app.lib.llm.anthropic import AnthropicLLMProvider  # noqa: E402

_CACHE = {"type": "ephemeral"}


class _Captured(Exception):
    pass


def _make_provider():
    captured: dict = {}

    class _Messages:
        async def create(self, **kwargs):
            captured.clear()
            captured.update(kwargs)
            raise _Captured()  # params recorded; skip response parsing

    class _Client:
        messages = _Messages()

    # __new__ bypasses __init__ so we don't build a real Anthropic client / need a key.
    p = AnthropicLLMProvider.__new__(AnthropicLLMProvider)
    p.model_name = "claude-sonnet-4-6"
    p.default_reasoning_effort = None
    p.client = _Client()
    return p, captured


async def _drain(agen):
    try:
        async for _ in agen:
            pass
    except Exception:
        pass


def _block_has_cache(block) -> bool:
    return isinstance(block, dict) and "cache_control" in block


def _msg_has_cache(msg) -> bool:
    content = msg.get("content")
    if isinstance(content, list):
        return any(_block_has_cache(b) for b in content)
    return False


_TOOLS = [
    {"type": "function", "function": {"name": "toolA", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "toolB", "parameters": {"type": "object", "properties": {}}}},
]
# [system, *history, volatile-turn] — the shape the reasoning loop builds.
_MSGS = [
    {"role": "system", "content": "SYSTEM PROMPT"},
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi there"},
    {"role": "user", "content": "INPUT SECTION"},
]


def test_cache_markers_placed_when_enabled():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(
        messages=_MSGS, tools=_TOOLS, retry=0, args={"prompt_cache": True},
    )))

    # system: wrapped as a single text block carrying cache_control
    system = captured["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == _CACHE

    # tools: only the LAST tool is marked
    tools = captured["tools"]
    assert tools[-1]["cache_control"] == _CACHE
    assert "cache_control" not in tools[0]

    # history: the last history message (messages[-2]) is marked; the volatile
    # final turn (messages[-1]) is not.
    messages = captured["messages"]
    assert _msg_has_cache(messages[-2])
    assert messages[-2]["content"][-1]["cache_control"] == _CACHE
    assert not _msg_has_cache(messages[-1])


def test_no_markers_when_disabled():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(
        messages=_MSGS, tools=_TOOLS, retry=0, args=None,
    )))

    assert isinstance(captured["system"], str)  # plain string, no cache block
    assert all("cache_control" not in t for t in captured["tools"])
    assert all(not _msg_has_cache(m) for m in captured["messages"])


def test_history_breakpoint_skipped_without_history():
    p, captured = _make_provider()
    # Only system + the volatile turn — no history to cache.
    msgs = [{"role": "system", "content": "SYSTEM PROMPT"}, {"role": "user", "content": "INPUT"}]
    asyncio.run(_drain(p.chat_completion(
        messages=msgs, tools=_TOOLS, retry=0, args={"prompt_cache": True},
    )))

    # system + last-tool markers still present...
    assert isinstance(captured["system"], list)
    assert captured["tools"][-1]["cache_control"] == _CACHE
    # ...but no history breakpoint: the single message is unmarked.
    assert len(captured["messages"]) == 1
    assert not _msg_has_cache(captured["messages"][0])


def test_prefix_byte_stable_across_steps():
    p, captured = _make_provider()

    step1 = _MSGS[:-1] + [{"role": "user", "content": "STEP ONE input"}]
    step2 = _MSGS[:-1] + [{"role": "user", "content": "STEP TWO different input"}]

    asyncio.run(_drain(p.chat_completion(messages=step1, tools=_TOOLS, retry=0, args={"prompt_cache": True})))
    params1 = copy.deepcopy(captured)
    asyncio.run(_drain(p.chat_completion(messages=step2, tools=_TOOLS, retry=0, args={"prompt_cache": True})))
    params2 = copy.deepcopy(captured)

    # The cached prefix (system + tools + history) must be byte-identical...
    assert params1["system"] == params2["system"]
    assert params1["tools"] == params2["tools"]
    assert params1["messages"][:-1] == params2["messages"][:-1]
    # ...while only the volatile final turn differs.
    assert params1["messages"][-1] != params2["messages"][-1]
