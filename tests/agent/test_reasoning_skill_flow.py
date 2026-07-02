"""Skill load + event-subscription flow on the reasoning agent.

After the rework:

- A skill's first call LOADS it: the recorded ``request`` arg is overwritten to a
  fixed marker and the full SKILL.md rides the call's ``role:"tool"`` result
  (untruncated), instead of being folded into the system prompt.
- "Loaded" is derived from the replayed history, so a repeat call short-circuits.
- Event subscription is folded onto the skill tool's own ``subscribe`` object and
  pinned to that exact skill — no active-skill state, no separate tool.
- Once a skill is loaded, its load affordance is removed from the ``tools=`` block
  (subscribe-only for event-bearing skills, dropped entirely otherwise) so it
  cannot be re-loaded. The block is byte-stable for a fixed loaded-skill set and
  changes exactly once per load; event enums come from metadata.
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
        self.tools_per_step: List[Optional[list]] = []  # tools= seen at each step

    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None,
                                     **kwargs) -> AsyncGenerator[dict, None]:
        self._step += 1
        self.tools_per_step.append(tools)
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
    assert args == {"request": "Load skill 'gmail' instructions (SKILL.md)"}

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
    # History already contains a load call for this skill (replayed trace). It
    # carries the OLD sentinel text on purpose: load detection is by args *shape*
    # (non-subscribe), not text, so pre-existing conversations stay recognized as
    # loaded after the sentinel was reworded.
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
    # The skill was loaded (derived from history), so its no-event stub is dropped
    # from the tools block — it physically cannot be re-loaded. The dispatch entry
    # still survived to route the model's (hallucinated) call to the short-circuit.
    step1_tools = llm.tools_per_step[0] or []
    assert all(t["function"]["name"] != "default__gmail" for t in step1_tools)


def test_skill_load_affordance_removed_within_same_turn(monkeypatch):
    """Loading a skill mid-turn removes its load affordance from the very next
    step's tools block, so a weak model cannot re-call it in a loop (the
    imap-email bug: gpt-4.1 re-called the loaded skill instead of acting on it)."""
    skill = _FakeSkillTool("default__gmail", "gmail", full_content="BODY")  # no events
    llm = _SkillCallLLM("default__gmail", {"request": "list my emails"})
    agent = _build_agent(monkeypatch, llm, [skill])

    _run(agent, "use gmail", history=[])

    assert len(llm.tools_per_step) >= 2
    step1 = llm.tools_per_step[0] or []
    step2 = llm.tools_per_step[1] or []
    # Loadable on the step that loads it...
    assert any(t["function"]["name"] == "default__gmail" for t in step1)
    # ...and gone on the next step (no events → dropped from the tools block).
    assert all(t["function"]["name"] != "default__gmail" for t in step2)


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
    assert args == {"request": "Load skill 'gmail' instructions (SKILL.md)"}


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


def test_event_skill_spec_becomes_subscribe_only_once_loaded(monkeypatch):
    gmail = _FakeSkillTool("default__gmail", "gmail",
                           events=[{"name": "new_email", "description": "new mail"}])
    agent = _spec_agent(monkeypatch, [gmail])

    # Unloaded: exposes `request` (load-and-use) plus `subscribe` (with the enum).
    specs_unloaded, _ = agent._build_tools_and_dispatch()
    props = specs_unloaded[0]["function"]["parameters"]["properties"]
    assert "request" in props
    assert props["subscribe"]["properties"]["trigger"]["items"]["enum"] == ["new_email"]

    # Loaded: the load affordance (`request`) is dropped so the skill cannot be
    # re-loaded; only `subscribe` remains (now required) so events still work.
    agent._loaded_skill_ids = {"default__gmail"}
    specs_loaded, dispatch = agent._build_tools_and_dispatch()
    fn = specs_loaded[0]["function"]
    assert fn["name"] == "default__gmail"
    loaded_props = fn["parameters"]["properties"]
    assert set(loaded_props) == {"subscribe"}
    assert "request" not in loaded_props
    assert fn["parameters"]["required"] == ["subscribe"]
    assert loaded_props["subscribe"]["properties"]["trigger"]["items"]["enum"] == ["new_email"]
    # Dispatch entry survives even though the load spec is gone, so a stray call
    # still routes to the already-loaded short-circuit rather than "Unknown tool".
    assert dispatch["default__gmail"][0] == "skill"


def test_event_triggered_run_keeps_subscribe_in_spec(monkeypatch):
    # The tools= block must be BYTE-IDENTICAL between event-triggered and normal
    # runs — it is the front of the prompt-cache prefix, so a schema divergence
    # here busts the cache on every event run. Storm prevention now happens at
    # dispatch time (see test_event_triggered_subscribe_is_refused), not by
    # dropping `subscribe` from the spec.
    gmail_evt = _FakeSkillTool("default__gmail", "gmail",
                               events=[{"name": "new_email", "description": "new mail"}])
    evt_specs, _ = _spec_agent(
        monkeypatch, [gmail_evt], triggered_by_event=True
    )._build_tools_and_dispatch()

    props = evt_specs[0]["function"]["parameters"]["properties"]
    assert "request" in props
    assert "subscribe" in props  # always exposed; refused at dispatch on event runs

    gmail_chat = _FakeSkillTool("default__gmail", "gmail",
                                events=[{"name": "new_email", "description": "new mail"}])
    chat_specs, _ = _spec_agent(
        monkeypatch, [gmail_chat], triggered_by_event=False
    )._build_tools_and_dispatch()
    assert evt_specs == chat_specs  # identical tools block → cache prefix reused


def test_event_triggered_subscribe_is_refused(monkeypatch):
    # On an event run the model may still emit a subscribe call (the schema is
    # identical), but it must be refused at runtime WITHOUT registering anything —
    # and the tool_call must still get a paired role:"tool" result so the replayed
    # trace has no dangling tool_use.
    called = {"n": 0}

    async def fake_register(**kwargs):  # pragma: no cover - must NOT be called
        called["n"] += 1
        return "SUBSCRIBED OK"

    monkeypatch.setattr(rse, "register_skill_events", fake_register)

    jira = _FakeSkillTool("default__jira", "jira",
                          events=[{"name": "issue_created", "description": "new issue"}])
    llm = _SkillCallLLM(
        "default__jira",
        {"subscribe": {"trigger": ["issue_created"], "action": "summarize the issue"}},
    )
    agent = _build_agent(monkeypatch, llm, [jira])
    agent._triggered_by_event = True

    chunks = _run(agent, "subscribe to jira issues", history=[])
    trace = _done(chunks)["llm_messages"]

    assert called["n"] == 0  # no subscription registered while handling an event
    assert trace[0]["role"] == "assistant"
    assert trace[0]["tool_calls"][0]["function"]["name"] == "default__jira"
    assert trace[1]["role"] == "tool"
    # Leading phrase only: _build_agent caps tool results at a tiny token budget
    # (proving results are truncated); the full refusal survives the real 1000-tok cap.
    assert "Subscriptions cannot be created" in trace[1]["content"]
