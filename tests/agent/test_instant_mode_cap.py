"""Instant mode caps a turn at ONE round of tool calls, then forces a text answer.

Instant mode offers tools normally on the first LLM call; if the model calls
tools, that single round runs, and the *next* call is forced with
``tool_choice="none"`` so the model must answer in plain text. An instant turn is
therefore at most 2 LLM calls / 1 tool round. Tools stay attached on the forced
step (Anthropic 400s on tool_use history without a tools param). A backend that
ignores ``tool_choice="none"`` and emits calls anyway must never trigger a second
tool round — those calls are dropped and the turn ends.

Reasoning mode is unaffected: it keeps looping with ``tool_choice="auto"``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import AsyncGenerator, List, Optional

import pytest

pytest.importorskip("a2a")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.tools.base import FunctionSpec, ToolResultEvent, ToolType  # noqa: E402
import app.agent.reasoning_agent as ra  # noqa: E402


# ── fakes ──────────────────────────────────────────────────────────────────

class _EchoLeafTool:
    """A built-in group with one executable leaf named ``calc__run``."""

    tool_type = ToolType.BUILTIN

    def __init__(self) -> None:
        self.tool_id = "calc"
        self.executed = 0

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        return [FunctionSpec(
            name="calc__run",
            leaf_name="run",
            schema={"type": "function", "function": {"name": "calc__run"}},
        )]

    async def execute_leaf(self, *, leaf_name, args, context_id, profile,
                           arguments, variables) -> AsyncGenerator[object, None]:
        self.executed += 1
        yield ToolResultEvent(observation_text="42")


class _ScriptedLLM:
    """Yields a scripted response per call, recording each call's kwargs.

    Each script entry is a tuple:
      ("tool", name)        → one tool call to ``name``, no text
      ("text", text)        → plain text answer, no tool call
      ("tool_text", name, t)→ text AND a tool call (a provider ignoring "none")
    """
    provider_name = "fake"
    model_name = "fake-model"
    model_label = "Fake fake-model"

    def __init__(self, script: List[tuple]) -> None:
        self._script = script
        self.calls: List[dict] = []  # captured kwargs per call

    async def chat_completion_stream(self, *, messages, tools=None,
                                     tool_choice=None, **kwargs) -> AsyncGenerator[dict, None]:
        idx = len(self.calls)
        self.calls.append({"tools": tools, "tool_choice": tool_choice, **kwargs})
        assert idx < len(self._script), (
            f"LLM called {idx + 1}x but the script only scripts {len(self._script)} call(s)"
        )
        action = self._script[idx]
        kind = action[0]
        if kind in ("tool", "tool_text"):
            if kind == "tool_text":
                yield {"type": ChatCompletionTypeEnum.CONTENT, "data": action[2]}
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [
                    {"index": 0, "id": f"call_{idx}", "name": action[1], "arguments": {}},
                ]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 2, "finish_reason": "tool_calls"}
        else:  # "text"
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": action[1]}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 3,
                   "output_tokens": 1, "finish_reason": "stop"}


def _build_agent(monkeypatch, llm, tools, *, mode: str) -> ra.ReasoningAgent:
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "generate_dir_tree", lambda p: "")

    async def _no_ltm(self):
        return ""
    monkeypatch.setattr(ra.ReasoningAgent, "_load_long_term_memory_block", _no_ltm)

    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.llm = llm
    agent.profile = "instant_test"
    agent.context_id = None
    agent.reasoning = mode != "instant"
    agent._mode = mode
    agent._plan_phase = None
    agent._triggered_by_event = False
    agent._event_run = False
    agent._inject_reasoning_guidance = False
    agent._tools = tools
    agent._tools_by_id = {t.tool_id: t for t in tools}
    agent.registry = SimpleNamespace(disabled_leaves_by_tool=lambda profile: {})
    agent.max_steps = 6
    agent._loaded_skill_ids = set()
    agent._total_input_tokens = 0
    agent._total_cache_read_input_tokens = 0
    agent._total_cache_creation_input_tokens = 0
    agent._total_output_tokens = 0
    agent._usage_records = []
    agent._reasoning_temperature = 1.0
    agent._reasoning_max_tokens = 1024
    agent._reasoning_retry = 0
    agent._max_llm_retries = 0
    agent._tool_result_enabled = True
    agent._tool_result_max_tokens = 4096
    agent._enable_prompt_cache = False
    return agent


def _run(agent) -> List[dict]:
    async def go() -> List[dict]:
        return [c async for c in agent.run("hi", history_messages=[])]
    return asyncio.run(go())


def _content(chunks: List[dict]) -> str:
    return "".join(
        c.get("data") or "" for c in chunks
        if c["type"] == ChatCompletionTypeEnum.CONTENT
    )


def _final_text(chunks: List[dict]) -> str:
    done = [c for c in chunks if c["type"] == ChatCompletionTypeEnum.DONE]
    assert len(done) == 1
    return done[0].get("data") or ""


# ── tests ────────────────────────────────────────────────────────────────

def test_instant_tool_round_then_forced_final(monkeypatch):
    leaf = _EchoLeafTool()
    llm = _ScriptedLLM([("tool", "calc__run"), ("text", "the answer")])
    agent = _build_agent(monkeypatch, llm, [leaf], mode="instant")

    chunks = _run(agent)

    # Exactly one tool round then a forced-final call.
    assert len(llm.calls) == 2
    assert llm.calls[0]["tool_choice"] == "auto"
    assert llm.calls[1]["tool_choice"] == "none"
    assert llm.calls[1]["tools"] is not None  # tools stay attached on the forced step
    assert leaf.executed == 1
    assert _content(chunks) == "the answer"


def test_instant_plain_text_is_single_call(monkeypatch):
    leaf = _EchoLeafTool()
    llm = _ScriptedLLM([("text", "hello")])
    agent = _build_agent(monkeypatch, llm, [leaf], mode="instant")

    chunks = _run(agent)

    # No tool call → pure single question→answer round, tools offered normally.
    assert len(llm.calls) == 1
    assert llm.calls[0]["tool_choice"] == "auto"
    assert leaf.executed == 0
    assert _content(chunks) == "hello"


def test_reasoning_mode_is_not_capped(monkeypatch):
    leaf = _EchoLeafTool()
    llm = _ScriptedLLM([
        ("tool", "calc__run"), ("tool", "calc__run"), ("text", "done"),
    ])
    agent = _build_agent(monkeypatch, llm, [leaf], mode="reasoning")

    chunks = _run(agent)

    # Reasoning keeps looping with tool_choice="auto"; two tool rounds run.
    assert len(llm.calls) == 3
    assert [c["tool_choice"] for c in llm.calls] == ["auto", "auto", "auto"]
    assert leaf.executed == 2
    assert _content(chunks) == "done"


def test_instant_ignored_none_with_text_uses_text(monkeypatch):
    # A backend that ignores tool_choice="none" returns tool calls again on the
    # forced step. The round-2 calls are dropped (not executed) and the streamed
    # text becomes the final answer.
    leaf = _EchoLeafTool()
    llm = _ScriptedLLM([("tool", "calc__run"), ("tool_text", "calc__run", "final text")])
    agent = _build_agent(monkeypatch, llm, [leaf], mode="instant")

    chunks = _run(agent)

    assert len(llm.calls) == 2
    assert leaf.executed == 1  # only the first round ran
    assert _content(chunks) == "final text"


def test_instant_ignored_none_no_text_falls_back(monkeypatch):
    # Same as above but the ignored forced step returns tool calls with NO text:
    # the turn ends with a short fallback message rather than a second tool round.
    leaf = _EchoLeafTool()
    llm = _ScriptedLLM([("tool", "calc__run"), ("tool", "calc__run")])
    agent = _build_agent(monkeypatch, llm, [leaf], mode="instant")

    chunks = _run(agent)

    assert len(llm.calls) == 2
    assert leaf.executed == 1
    assert "one-round tool limit" in _final_text(chunks)
