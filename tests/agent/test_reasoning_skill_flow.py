"""Skill load + event-subscription flow on the reasoning agent.

After the rework:

- A skill's first call LOADS it: the recorded ``request`` arg is overwritten to a
  fixed marker and the full SKILL.md rides the call's ``role:"tool"`` result
  (untruncated), instead of being folded into the system prompt.
- "Loaded" is derived from the replayed history, so a repeat call short-circuits.
- Event subscription is folded onto the skill tool's own ``subscribe`` object and
  pinned to that exact skill — no active-skill state, no separate tool.
- Skill specs are static (loaded skills are NOT removed; event enums come from
  metadata), keeping the ``tools=`` block byte-stable for prompt caching.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List, Optional

import pytest

pytest.importorskip("a2a")

from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.tools.base import Tool, ToolType  # noqa: E402
import app.agent.reasoning_agent as ra  # noqa: E402
import app.tools.builtin.register_skill_event as rse  # noqa: E402


# ── fakes ──────────────────────────────────────────────────────────────────

class _FakeSkillInfo:
    def __init__(self, dir_path: Path, full_content: str, metadata: dict):
        self.dir_path = dir_path
        self.full_content = full_content
        self.metadata = metadata


class _FakeSkillTool(Tool):
    tool_type = ToolType.SKILL

    def __init__(self, tool_id: str, name: str, *, full_content: str = "",
                 events: Optional[List[dict]] = None):
        super().__init__()
        self.tool_id = tool_id
        self._name = name
        metadata: Dict[str, Any] = {}
        if events is not None:
            metadata["events"] = {"event_type": events}
        self._info = _FakeSkillInfo(Path("/skills") / name, full_content, metadata)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"The {self._name} skill."

    @property
    def info(self) -> _FakeSkillInfo:
        return self._info

    async def execute(self, *, query, context_id, profile, arguments,
                      variables, llm_params):  # pragma: no cover - unused
        if False:
            yield None  # type: ignore[unreachable]


class _SkillCallLLM:
    """Calls one skill tool on step 1 (with the given args), then answers."""
    provider_name = "fake"
    model_name = "fake-model"
    model_label = "Fake fake-model"

    def __init__(self, skill_id: str, call_args: dict):
        self._step = 0
        self._skill_id = skill_id
        self._call_args = call_args

    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None,
                                     **kwargs) -> AsyncGenerator[dict, None]:
        self._step += 1
        if self._step == 1:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [
                    {"index": 0, "id": "call_1", "name": self._skill_id,
                     "arguments": self._call_args},
                ]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 2, "finish_reason": "tool_calls"}
        else:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": "done"}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 3,
                   "output_tokens": 1, "finish_reason": "stop"}


def _build_agent(monkeypatch, llm, tools: List[Tool]) -> ra.ReasoningAgent:
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    # generate_dir_tree would hit the filesystem for the fake dir; stub it out.
    monkeypatch.setattr(ra, "generate_dir_tree", lambda p: "")

    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.llm = llm
    agent.profile = "default"
    agent.context_id = None  # skips cwd-anchor + ContextStorage mirror branches
    agent.reasoning = True
    agent._triggered_by_event = False
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
    agent._tool_result_max_tokens = 5  # tiny: proves the load result is NOT truncated
    agent._enable_prompt_cache = False
    return agent


def _run(agent, query, history) -> List[dict]:
    async def go() -> List[dict]:
        return [c async for c in agent.run(query, history_messages=history)]
    return asyncio.run(go())


def _done(chunks: List[dict]) -> dict:
    done = [c for c in chunks if c["type"] == ChatCompletionTypeEnum.DONE]
    assert len(done) == 1
    return done[0]


# ── load path ────────────────────────────────────────────────────────────

def test_skill_load_overwrites_request_and_returns_full_content(monkeypatch):
    long_body = "SKILL BODY LINE\n" * 200  # far beyond the tiny truncation cap
    skill = _FakeSkillTool("default__gmail", "gmail", full_content=long_body)
    llm = _SkillCallLLM("default__gmail", {"request": "list my emails for today"})
    agent = _build_agent(monkeypatch, llm, [skill])

    chunks = _run(agent, "use gmail", history=[])
    trace = _done(chunks)["llm_messages"]

    # The recorded tool call's request was overwritten to the fixed marker,
    # regardless of what the model originally typed.
    assert trace[0]["role"] == "assistant"
    args = json.loads(trace[0]["tool_calls"][0]["function"]["arguments"])
    assert args == {"request": "You need to load the SKILL.md file for skill gmail"}

    # The full SKILL.md content rides the tool result, untruncated, with the
    # usage guidance that used to live in the system prompt.
    tool_msg = trace[1]
    assert tool_msg["role"] == "tool"
    assert long_body in tool_msg["content"]
    assert "`default__gmail`" in tool_msg["content"]
    assert "Exec Shell" in tool_msg["content"]


def test_already_loaded_skill_short_circuits(monkeypatch):
    body = "SECRET SKILL INSTRUCTIONS"
    skill = _FakeSkillTool("default__gmail", "gmail", full_content=body)
    # History already contains a load call for this skill (replayed trace).
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "default__gmail",
                         "arguments": json.dumps({"request": "You need to load the SKILL.md file for skill gmail"})},
        }]},
        {"role": "tool", "tool_call_id": "p1", "content": body},
    ]
    llm = _SkillCallLLM("default__gmail", {"request": "use it again"})
    agent = _build_agent(monkeypatch, llm, [skill])

    chunks = _run(agent, "use gmail again", history=history)
    trace = _done(chunks)["llm_messages"]

    tool_msg = trace[1]
    assert tool_msg["role"] == "tool"
    assert "already loaded" in tool_msg["content"]
    # The full content is NOT re-emitted (it is already above in history).
    assert body not in tool_msg["content"]


def test_skill_load_thinking_artifact_shows_marker(monkeypatch):
    """The UI Thinking Process shows the same fixed marker the trace is overwritten
    to — not whatever the model originally typed in ``request``."""
    skill = _FakeSkillTool("default__gmail", "gmail", full_content="BODY")
    llm = _SkillCallLLM("default__gmail", {"request": "list my emails for today"})
    agent = _build_agent(monkeypatch, llm, [skill])

    chunks = _run(agent, "use gmail", history=[])
    thinks = [c for c in chunks if c["type"] == ChatCompletionTypeEnum.THINKING_ARTIFACT]
    skill_think = [t for t in thinks if t["data"]["Tool"] == "default__gmail"]
    assert skill_think
    args = json.loads(skill_think[0]["data"]["Tool_Input"])
    assert args == {"request": "You need to load the SKILL.md file for skill gmail"}


# ── subscribe path ──────────────────────────────────────────────────────────

def test_subscribe_routes_to_register_for_the_called_skill(monkeypatch):
    recorded: Dict[str, Any] = {}

    async def fake_register(**kwargs):
        recorded.update(kwargs)
        return "SUBSCRIBED OK"

    monkeypatch.setattr(rse, "register_skill_events", fake_register)

    gmail = _FakeSkillTool("default__gmail", "gmail",
                           events=[{"name": "new_email", "description": "new mail"}])
    jira = _FakeSkillTool("default__jira", "jira",
                          events=[{"name": "issue_created", "description": "new issue"}])
    # gmail is already loaded; we still subscribe an event on jira (the *called*
    # skill) — proving there is no active-skill limitation.
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "p1", "type": "function",
            "function": {"name": "default__gmail",
                         "arguments": json.dumps({"request": "x"})},
        }]},
        {"role": "tool", "tool_call_id": "p1", "content": "gmail body"},
    ]
    llm = _SkillCallLLM(
        "default__jira",
        {"subscribe": {"trigger": ["issue_created"], "action": "summarize the issue"}},
    )
    agent = _build_agent(monkeypatch, llm, [gmail, jira])

    chunks = _run(agent, "whenever a jira issue is created, summarize it", history=history)
    trace = _done(chunks)["llm_messages"]

    assert recorded["skill_id"] == "default__jira"
    assert recorded["triggers"] == ["issue_created"]
    assert recorded["action"] == "summarize the issue"
    assert trace[1]["role"] == "tool"
    assert trace[1]["content"] == "SUBSCRIBED OK"

    # A subscribe call shows its payload verbatim in the UI (not the load marker).
    thinks = [c for c in chunks if c["type"] == ChatCompletionTypeEnum.THINKING_ARTIFACT]
    jira_think = [t for t in thinks if t["data"]["Tool"] == "default__jira"]
    assert jira_think
    shown = json.loads(jira_think[0]["data"]["Tool_Input"])
    assert shown == {"subscribe": {"trigger": ["issue_created"], "action": "summarize the issue"}}


# ── spec stability + event enum ──────────────────────────────────────────────

def _spec_agent(monkeypatch, tools, *, triggered_by_event=False):
    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.profile = "default"
    agent._tools = tools
    agent._tools_by_id = {t.tool_id: t for t in tools}
    agent._triggered_by_event = triggered_by_event
    agent._loaded_skill_ids = set()
    agent._current_query = ""
    agent.registry = SimpleNamespace(disabled_leaves_by_tool=lambda profile: {})
    return agent


def test_skill_spec_is_stable_and_carries_event_enum(monkeypatch):
    gmail = _FakeSkillTool("default__gmail", "gmail",
                           events=[{"name": "new_email", "description": "new mail"}])
    agent = _spec_agent(monkeypatch, [gmail])

    specs1, _ = agent._build_tools_and_dispatch()
    # Marking the skill loaded must NOT change the spec (no exclusion) — the
    # tools block stays byte-stable across turns for prompt caching.
    agent._loaded_skill_ids = {"default__gmail"}
    specs2, _ = agent._build_tools_and_dispatch()
    assert specs1 == specs2

    fn = specs1[0]["function"]
    assert fn["name"] == "default__gmail"
    props = fn["parameters"]["properties"]
    assert "request" in props
    assert props["subscribe"]["properties"]["trigger"]["items"]["enum"] == ["new_email"]


def test_event_triggered_run_omits_subscribe(monkeypatch):
    gmail = _FakeSkillTool("default__gmail", "gmail",
                           events=[{"name": "new_email", "description": "new mail"}])
    agent = _spec_agent(monkeypatch, [gmail], triggered_by_event=True)

    specs, _ = agent._build_tools_and_dispatch()
    props = specs[0]["function"]["parameters"]["properties"]
    assert "request" in props
    assert "subscribe" not in props  # no new subscriptions during event-triggered runs
