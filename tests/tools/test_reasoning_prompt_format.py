"""Native function-calling loop: the reasoning agent calls leaf tools directly,
feeds results back as role:"tool" messages, and ends the turn on plain text.

A scripted fake LLM emits a tool call on the first step and a final text answer
on the second; a fake one-leaf tool records its invocation. We assert: the leaf
ran with the model's typed args, the final answer streamed as CONTENT, the turn
ended with a DONE, and per-call THINKING/RESULT artifacts were emitted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List

import pytest

pytest.importorskip("a2a")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.tools.base import (  # noqa: E402
    FunctionSpec,
    Tool,
    ToolResultEvent,
    ToolType,
)
import app.agent.reasoning_agent as ra  # noqa: E402


class _FakeLeafTool(Tool):
    tool_type = ToolType.BUILTIN

    def __init__(self) -> None:
        super().__init__()
        self._tool_id = "calc"
        self.calls: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "Calculator"

    @property
    def description(self) -> str:
        return "Adds numbers."

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        return [FunctionSpec(
            name="calc__add",
            leaf_name="add",
            schema={
                "type": "function",
                "function": {
                    "name": "calc__add",
                    "description": "Add a and b.",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"],
                    },
                },
            },
        )]

    async def execute(self, *, query, context_id, profile, arguments,
                      variables):  # pragma: no cover - unused
        if False:
            yield None  # type: ignore[unreachable]

    async def execute_leaf(self, *, leaf_name, args, context_id, profile,
                           arguments, variables):
        self.calls.append({"leaf": leaf_name, "args": args})
        total = (args.get("a") or 0) + (args.get("b") or 0)
        yield ToolResultEvent(observation_text=f"result={total}")


class _ScriptedLLM:
    """Yields a tool call on step 1, then streams a final text answer on step 2."""
    provider_name = "fake"
    model_name = "fake-model"
    model_label = "Fake fake-model"

    def __init__(self) -> None:
        self._step = 0

    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None,
                                     **kwargs) -> AsyncGenerator[dict, None]:
        self._step += 1
        if self._step == 1:
            # The model picks the namespaced leaf with typed args.
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [
                    {"index": 0, "id": "call_1", "name": "calc__add",
                     "arguments": {"a": 2, "b": 3}},
                ]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 2, "finish_reason": "tool_calls"}
        else:
            # No tool calls -> the streamed text is the final answer.
            for tok in ("The ", "answer ", "is 5."):
                yield {"type": ChatCompletionTypeEnum.CONTENT, "data": tok}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 7,
                   "output_tokens": 3, "finish_reason": "stop"}


def _build_agent(monkeypatch) -> tuple[ra.ReasoningAgent, _FakeLeafTool]:
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")

    tool = _FakeLeafTool()
    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.llm = _ScriptedLLM()
    agent.profile = "default"
    agent.context_id = None
    agent.reasoning = True
    agent._memory_context = ""
    agent._triggered_by_event = False
    agent._inject_reasoning_guidance = False
    agent._tools = [tool]
    agent._tools_by_id = {"calc": tool}
    # No sub-tools disabled for this profile (real agents resolve this via the
    # registry; here _tools is set directly so a minimal stub suffices).
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
    agent._tool_result_enabled = False
    agent._tool_result_max_tokens = 4096
    agent._enable_prompt_cache = False
    return agent, tool


def test_native_fc_loop_runs_leaf_then_streams_final_answer(monkeypatch) -> None:
    agent, tool = _build_agent(monkeypatch)

    async def _run() -> List[dict]:
        return [c async for c in agent.run("add 2 and 3", history_messages=[])]

    chunks = asyncio.run(_run())
    types = [c["type"] for c in chunks]

    # The leaf executed with the model's typed args.
    assert tool.calls == [{"leaf": "add", "args": {"a": 2, "b": 3}}]

    # UI artifacts were emitted for the tool call.
    assert ChatCompletionTypeEnum.THINKING_ARTIFACT in types
    assert ChatCompletionTypeEnum.RESULT_ARTIFACT in types

    # The final answer streamed as CONTENT deltas (real streaming).
    streamed = "".join(
        c["data"] for c in chunks if c["type"] == ChatCompletionTypeEnum.CONTENT
    )
    assert streamed == "The answer is 5."

    # The turn ended with a DONE; the streamed answer is not duplicated in data.
    done = [c for c in chunks if c["type"] == ChatCompletionTypeEnum.DONE]
    assert len(done) == 1
    assert done[0]["data"] == ""


def test_tool_result_fed_back_as_tool_message(monkeypatch) -> None:
    agent, _tool = _build_agent(monkeypatch)

    async def _run() -> None:
        async for _ in agent.run("add 2 and 3", history_messages=[]):
            pass

    asyncio.run(_run())
    # After the run, the in-turn trace holds the assistant tool_call + a tool result.
    roles = [m["role"] for m in agent._turn_messages]
    assert roles == ["assistant", "tool"]
    assert agent._turn_messages[0]["tool_calls"][0]["function"]["name"] == "calc__add"
    assert "result=5" in agent._turn_messages[1]["content"]


class _TwoLeafTool(_FakeLeafTool):
    """A group exposing two leaves so a step can call both in parallel."""

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        out = []
        for leaf in ("add", "mul"):
            out.append(FunctionSpec(
                name=f"calc__{leaf}", leaf_name=leaf,
                schema={"type": "function", "function": {
                    "name": f"calc__{leaf}",
                    "parameters": {"type": "object",
                                   "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                                   "required": ["a", "b"]},
                }},
            ))
        return out

    async def execute_leaf(self, *, leaf_name, args, context_id, profile,
                           arguments, variables):
        self.calls.append({"leaf": leaf_name, "args": args})
        a, b = (args.get("a") or 0), (args.get("b") or 0)
        total = a + b if leaf_name == "add" else a * b
        yield ToolResultEvent(observation_text=f"{leaf_name}={total}")


class _TwoCallLLM(_ScriptedLLM):
    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None, **kwargs):
        self._step += 1
        if self._step == 1:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [
                    {"index": 0, "id": "c1", "name": "calc__add", "arguments": {"a": 2, "b": 3}},
                    {"index": 1, "id": "c2", "name": "calc__mul", "arguments": {"a": 4, "b": 5}},
                ]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 4, "finish_reason": "tool_calls"}
        else:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": "done"}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 6,
                   "output_tokens": 1, "finish_reason": "stop"}


def test_parallel_tool_calls_run_and_group_in_one_step(monkeypatch) -> None:
    agent, _ = _build_agent(monkeypatch)
    tool = _TwoLeafTool()
    agent.llm = _TwoCallLLM()
    agent._tools = [tool]
    agent._tools_by_id = {"calc": tool}

    async def _run():
        return [c async for c in agent.run("add 2+3 and multiply 4*5", history_messages=[])]

    chunks = asyncio.run(_run())

    # Both leaves ran (in one step).
    assert {c["leaf"] for c in tool.calls} == {"add", "mul"}

    # Two THINKING artifacts, sharing the same Step number.
    thinks = [c["data"] for c in chunks if c["type"] == ChatCompletionTypeEnum.THINKING_ARTIFACT]
    assert len(thinks) == 2
    assert thinks[0]["Step"] == thinks[1]["Step"]
    assert {t["Tool"] for t in thinks} == {"calc__add", "calc__mul"}

    # Assistant message carried both tool_calls; both tool results were appended.
    roles = [m["role"] for m in agent._turn_messages]
    assert roles == ["assistant", "tool", "tool"]
    tool_texts = " ".join(m["content"] for m in agent._turn_messages if m["role"] == "tool")
    assert "add=5" in tool_texts and "mul=20" in tool_texts


# ── event-triggered storm prevention: register_file_watcher ───────────────────
#
# The register_file_watcher subtool stays in the schema on EVERY run (the tools=
# block must be byte-identical for prompt-cache reuse), but its EXECUTION is
# blocked while reacting to an event to avoid recursive event storms. The blocked
# call must still get a paired role:"tool" result (no dangling tool_use on replay).

class _FakeWatcherTool(_FakeLeafTool):
    """A system_file-like group exposing the register_file_watcher leaf."""

    def __init__(self) -> None:
        super().__init__()
        self._tool_id = "system_file"

    @property
    def name(self) -> str:
        return "System File"

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        return [FunctionSpec(
            name="system_file__register_file_watcher",
            leaf_name="register_file_watcher",
            schema={"type": "function", "function": {
                "name": "system_file__register_file_watcher",
                "parameters": {"type": "object", "properties": {}},
            }},
        )]

    async def execute_leaf(self, *, leaf_name, args, context_id, profile,
                           arguments, variables):
        self.calls.append({"leaf": leaf_name, "args": args})
        yield ToolResultEvent(observation_text="WATCHER REGISTERED")


class _WatcherCallLLM(_ScriptedLLM):
    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None, **kwargs):
        self._step += 1
        if self._step == 1:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [
                    {"index": 0, "id": "w1",
                     "name": "system_file__register_file_watcher",
                     "arguments": {"path": ".", "action": "do x"}},
                ]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 2, "finish_reason": "tool_calls"}
        else:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": "ok"}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 3,
                   "output_tokens": 1, "finish_reason": "stop"}


def _build_watcher_agent(monkeypatch):
    agent, _ = _build_agent(monkeypatch)
    tool = _FakeWatcherTool()
    agent.llm = _WatcherCallLLM()
    agent._tools = [tool]
    agent._tools_by_id = {"system_file": tool}
    return agent, tool


def test_event_run_blocks_register_file_watcher(monkeypatch) -> None:
    agent, tool = _build_watcher_agent(monkeypatch)
    # A real event run carries BOTH flags (the dispatcher passes a trigger_event
    # and event_run=True). ``_event_run`` is the one that blocks unconditionally.
    agent._triggered_by_event = True
    agent._event_run = True

    async def _run():
        return [c async for c in agent.run("watch this folder", history_messages=[])]

    asyncio.run(_run())

    # The leaf was NOT executed...
    assert tool.calls == []
    # ...but the tool_call still got a paired role:"tool" refusal (replay-safe).
    roles = [m["role"] for m in agent._turn_messages]
    assert roles[:2] == ["assistant", "tool"]
    assert "not allowed from inside an event-triggered run" in agent._turn_messages[1]["content"]


def test_task_continuation_turn_blocks_standing_file_watcher(monkeypatch) -> None:
    """A turn continuing an event task may not register a STANDING watcher.

    ``_triggered_by_event`` without ``_event_run`` is the event-task
    continuation turn: an ordinary chat turn resuming a flow. It may register
    one more one-shot task, but a standing rule would re-register itself on
    every future result.
    """
    agent, tool = _build_watcher_agent(monkeypatch)
    agent._triggered_by_event = True  # _event_run stays False

    async def _run():
        return [c async for c in agent.run("watch this folder", history_messages=[])]

    asyncio.run(_run())

    assert tool.calls == []
    assert "only ONE-SHOT TASKS" in agent._turn_messages[1]["content"]


def test_normal_run_allows_register_file_watcher(monkeypatch) -> None:
    agent, tool = _build_watcher_agent(monkeypatch)
    # _triggered_by_event stays False (set by _build_agent) → the leaf runs.

    async def _run():
        async for _ in agent.run("watch this folder", history_messages=[]):
            pass

    asyncio.run(_run())
    assert tool.calls == [
        {"leaf": "register_file_watcher", "args": {"path": ".", "action": "do x"}}
    ]


# ── event-triggered storm prevention: schedule_create ─────────────────────────
#
# Same contract as register_file_watcher: schedule_create stays in the schema on
# every run but its EXECUTION is blocked inside an event run so an action can't
# register another schedule on every fire.

class _FakeSchedulerTool(_FakeLeafTool):
    """A scheduler-like group exposing the schedule_create leaf."""

    def __init__(self) -> None:
        super().__init__()
        self._tool_id = "scheduler"

    @property
    def name(self) -> str:
        return "Scheduler"

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        return [FunctionSpec(
            name="scheduler__schedule_create",
            leaf_name="schedule_create",
            schema={"type": "function", "function": {
                "name": "scheduler__schedule_create",
                "parameters": {"type": "object", "properties": {}},
            }},
        )]

    async def execute_leaf(self, *, leaf_name, args, context_id, profile,
                           arguments, variables):
        self.calls.append({"leaf": leaf_name, "args": args})
        yield ToolResultEvent(observation_text="SCHEDULE CREATED")


class _ScheduleCallLLM(_ScriptedLLM):
    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None, **kwargs):
        self._step += 1
        if self._step == 1:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [
                    {"index": 0, "id": "s1",
                     "name": "scheduler__schedule_create",
                     "arguments": {"title": "t", "dtstart": "2026-07-10T09:00:00",
                                   "action": "do x"}},
                ]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 2, "finish_reason": "tool_calls"}
        else:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": "ok"}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 3,
                   "output_tokens": 1, "finish_reason": "stop"}


def _build_scheduler_agent(monkeypatch):
    agent, _ = _build_agent(monkeypatch)
    tool = _FakeSchedulerTool()
    agent.llm = _ScheduleCallLLM()
    agent._tools = [tool]
    agent._tools_by_id = {"scheduler": tool}
    return agent, tool


def test_event_run_blocks_schedule_create(monkeypatch) -> None:
    agent, tool = _build_scheduler_agent(monkeypatch)
    agent._triggered_by_event = True
    agent._event_run = True

    async def _run():
        return [c async for c in agent.run("schedule this", history_messages=[])]

    asyncio.run(_run())

    # The leaf was NOT executed...
    assert tool.calls == []
    # ...but the tool_call still got a paired role:"tool" refusal (replay-safe).
    roles = [m["role"] for m in agent._turn_messages]
    assert roles[:2] == ["assistant", "tool"]
    assert "not allowed from inside an event-triggered run" in agent._turn_messages[1]["content"]


def test_task_continuation_turn_allows_one_shot_schedule(monkeypatch) -> None:
    """A turn continuing an event task MAY register one more one-shot task.

    This is the chaining case ("reply to the customer, wait for their answer,
    then follow up"): the scripted call has no rrule and a default duration, so
    it is a one-shot instant event — an event task — and must go through.
    """
    agent, tool = _build_scheduler_agent(monkeypatch)
    agent._triggered_by_event = True  # _event_run stays False

    async def _run():
        async for _ in agent.run("schedule this", history_messages=[]):
            pass

    asyncio.run(_run())
    assert tool.calls == [{
        "leaf": "schedule_create",
        "args": {"title": "t", "dtstart": "2026-07-10T09:00:00", "action": "do x"},
    }]


def test_task_chain_depth_cap_stops_a_runaway_wait_loop(monkeypatch) -> None:
    """A wait→continue→wait flow could otherwise self-perpetuate forever.

    The cap is dispatch-time only (it never reaches the prompt), so it costs no
    prompt cache — but past the limit even a one-shot task is refused and the
    assistant has to hand back to the user.
    """
    from app.events.task_policy import MAX_TASK_CHAIN_DEPTH

    agent, tool = _build_scheduler_agent(monkeypatch)
    agent._triggered_by_event = True
    agent._task_chain_depth = MAX_TASK_CHAIN_DEPTH

    async def _run():
        return [c async for c in agent.run("schedule this", history_messages=[])]

    asyncio.run(_run())
    assert tool.calls == []
    assert "already chained" in agent._turn_messages[1]["content"]


def test_a_task_continuation_turn_still_sees_the_registration_tools(monkeypatch) -> None:
    """The tools block must be byte-identical to an ordinary chat turn's.

    The continuation turn is the first turn that is event-TRIGGERED while living
    in a normal chat conversation. Gating the schema on that (rather than on the
    conversation-constant event-run flag) would both bust the chat's cached
    prefix mid-conversation AND hide the very tools this turn is allowed to call.
    """
    def _specs(**flags):
        agent, _ = _build_scheduler_agent(monkeypatch)
        agent._current_query = "schedule this"
        for k, v in flags.items():
            setattr(agent, k, v)
        return agent._build_tools_and_dispatch()

    chat_specs, _ = _specs()
    cont_specs, _ = _specs(_triggered_by_event=True)

    assert cont_specs == chat_specs
    assert any(
        s["function"]["name"] == "scheduler__schedule_create" for s in cont_specs
    )

    # An event run is a separate cache population and DOES hide them.
    run_specs, run_dispatch = _specs(_triggered_by_event=True, _event_run=True)
    assert run_specs == []
    # ...but the dispatch entry survives, so a replayed call is refused, not
    # answered with "Unknown tool".
    assert "scheduler__schedule_create" in run_dispatch


def test_normal_run_allows_schedule_create(monkeypatch) -> None:
    agent, tool = _build_scheduler_agent(monkeypatch)
    # _triggered_by_event stays False (set by _build_agent) → the leaf runs.

    async def _run():
        async for _ in agent.run("schedule this", history_messages=[]):
            pass

    asyncio.run(_run())
    assert tool.calls == [
        {"leaf": "schedule_create",
         "args": {"title": "t", "dtstart": "2026-07-10T09:00:00", "action": "do x"}}
    ]
