"""Anthropic must honor ``parallel_tool_calls``.

Anthropic runs tools in parallel by default, so the request must add
``disable_parallel_tool_use`` to ``tool_choice`` *only* when ``parallel_tool_calls``
is explicitly ``False`` — ``True``/``None`` leave the parallel-on default untouched.
The shared ``_anthropic_tool_choice`` helper (used by both the streaming and
non-streaming paths) is unit-tested directly; the request-construction is captured
by stubbing ``client.messages.create`` on the non-streaming path.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("openai")

from app.lib.llm.anthropic import AnthropicLLMProvider, _anthropic_tool_choice  # noqa: E402


# --- direct helper unit tests (covers both provider methods' shared logic) ----

def test_helper_maps_tool_choice():
    assert _anthropic_tool_choice("auto", None) == {"type": "auto"}
    assert _anthropic_tool_choice(None, None) == {"type": "auto"}
    assert _anthropic_tool_choice("required", None) == {"type": "any"}
    # "none" keeps tools attached but forbids calls (Instant mode's forced-final
    # step relies on this — tool_use history 400s without a tools param).
    assert _anthropic_tool_choice("none", None) == {"type": "none"}
    assert _anthropic_tool_choice(
        {"type": "function", "function": {"name": "f"}}, None
    ) == {"type": "tool", "name": "f"}


def test_helper_adds_disable_only_when_false():
    assert _anthropic_tool_choice("auto", True) == {"type": "auto"}
    assert _anthropic_tool_choice("auto", False) == {
        "type": "auto", "disable_parallel_tool_use": True,
    }
    assert _anthropic_tool_choice("required", False) == {
        "type": "any", "disable_parallel_tool_use": True,
    }
    # "none" never carries the disable flag (documented only for auto/any/tool).
    assert _anthropic_tool_choice("none", False) == {"type": "none"}


# --- request-construction test via the provider -------------------------------

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


_TOOLS = [{
    "type": "function",
    "function": {"name": "toolA", "parameters": {"type": "object", "properties": {}}},
}]
_MSGS = [{"role": "user", "content": "x"}]


def test_default_none_omits_disable_flag():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(messages=_MSGS, tools=_TOOLS, tool_choice="auto")))
    assert captured["tool_choice"] == {"type": "auto"}


def test_true_omits_disable_flag():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(
        messages=_MSGS, tools=_TOOLS, tool_choice="auto", parallel_tool_calls=True,
    )))
    assert "disable_parallel_tool_use" not in captured["tool_choice"]


def test_false_sets_disable_flag():
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(
        messages=_MSGS, tools=_TOOLS, tool_choice="auto", parallel_tool_calls=False,
    )))
    assert captured["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}


def test_none_keeps_tools_attached():
    # Instant mode's forced-final step: tools must stay in the request (Anthropic
    # 400s on tool_use history without them) while tool_choice forbids new calls.
    p, captured = _make_provider()
    asyncio.run(_drain(p.chat_completion(
        messages=_MSGS, tools=_TOOLS, tool_choice="none",
    )))
    assert captured["tool_choice"] == {"type": "none"}
    assert captured["tools"]  # tools remain attached
