"""ReasoningAgent gates plan-mode tools + guidance by mode/phase, and instant
mode drops the think-tool. Reasoning-mode runs must stay byte-identical to today
(prompt-cache invariant).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


class _FakeTool:
    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id


class _FakeRegistry:
    def __init__(self, tools) -> None:
        self._tools = tools

    def tools_for_profile(self, profile):
        return list(self._tools)

    def disabled_leaves_by_tool(self, profile):
        return {}


def _fake_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        max_llm_retries=0,
        reasoning_temperature=1.0,
        reasoning_max_tokens=1024,
        reasoning_retry=0,
        tool_result_enabled=False,
        tool_result_max_tokens=4096,
        enable_prompt_cache=False,
        max_steps=6,
    )


_ALL_TOOLS = ["reasoning", "ask_user_question", "write_plan", "update_todos", "calc"]


def _build(monkeypatch, provider="fake", model="fake-model", *, mode="reasoning", plan_phase=None, event_run=False):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name=provider, model_name=model)
    registry = _FakeRegistry([_FakeTool(t) for t in _ALL_TOOLS])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile="default", context_id="ctx",
        mode=mode, plan_phase=plan_phase, event_run=event_run,
    )


def test_reasoning_mode_strips_all_plan_tools(monkeypatch):
    agent = _build(monkeypatch, mode="reasoning")
    for t in ("ask_user_question", "write_plan", "update_todos"):
        assert t not in agent._tools_by_id
    assert "calc" in agent._tools_by_id
    # No plan guidance leaks into a normal run.
    prompt = agent._build_instruction()
    assert "PLAN MODE" not in prompt


def test_plan_planning_phase_exposes_planning_tools(monkeypatch):
    agent = _build(monkeypatch, mode="plan", plan_phase="planning")
    assert "ask_user_question" in agent._tools_by_id
    assert "write_plan" in agent._tools_by_id
    assert "update_todos" in agent._tools_by_id  # available so "implement it" can execute
    prompt = agent._build_instruction()
    assert "PLAN MODE — PLANNING PHASE" in prompt
    # planning forces one-tool-per-step
    assert agent._parallel_tool_calls is False


def test_plan_execute_phase_exposes_only_update_todos(monkeypatch):
    agent = _build(monkeypatch, mode="plan", plan_phase="execute")
    assert "update_todos" in agent._tools_by_id
    assert "ask_user_question" not in agent._tools_by_id
    assert "write_plan" not in agent._tools_by_id
    prompt = agent._build_instruction()
    assert "PLAN MODE — EXECUTION PHASE" in prompt


def test_planning_prompt_has_automation_branch(monkeypatch):
    agent = _build(monkeypatch, mode="plan", plan_phase="planning")
    assert "AUTOMATION REQUESTS" in agent._build_instruction()


def test_execution_prompt_has_automation_branch(monkeypatch):
    agent = _build(monkeypatch, mode="plan", plan_phase="execute")
    assert "AUTOMATION PLANS ARE DIFFERENT" in agent._build_instruction()


def test_event_run_exposes_update_todos(monkeypatch):
    # A multi-step event action drives a live todo panel: update_todos is exposed
    # on event runs, but the plan authoring tools never are.
    agent = _build(monkeypatch, mode="reasoning", event_run=True)
    assert "update_todos" in agent._tools_by_id
    assert "ask_user_question" not in agent._tools_by_id
    assert "write_plan" not in agent._tools_by_id


def test_reasoning_chat_run_never_sees_update_todos(monkeypatch):
    # Ordinary (non-event, non-plan) chat runs must stay byte-identical to today.
    agent = _build(monkeypatch, mode="reasoning", event_run=False)
    assert "update_todos" not in agent._tools_by_id


def test_instant_mode_drops_think_tool_and_guidance(monkeypatch):
    # fake/fake-model is non-native-reasoning, so normally the think-tool stays.
    baseline = _build(monkeypatch, mode="reasoning")
    assert "reasoning" in baseline._tools_by_id

    agent = _build(monkeypatch, mode="instant")
    assert "reasoning" not in agent._tools_by_id
    assert agent._inject_reasoning_guidance is False
    assert "REASONING STEP" not in agent._build_instruction()


def test_reasoning_mode_prompt_unchanged_by_feature(monkeypatch):
    # The reasoning-mode prompt must not contain any new plan/instant text.
    agent = _build(monkeypatch, mode="reasoning")
    prompt = agent._build_instruction()
    assert "PLAN MODE" not in prompt
    # think-tool + its guidance still present for a non-native model (today's behavior)
    assert "reasoning" in agent._tools_by_id
    assert "REASONING STEP" in prompt


# ── read-only enforcement (dispatch-time block) ────────────────────────────

def _leaf(tool_id, leaf_name):
    return ("leaf", _FakeTool(tool_id), leaf_name)


def test_planning_phase_blocks_mutating_leaves(monkeypatch):
    agent = _build(monkeypatch, mode="plan", plan_phase="planning")
    # Mutating leaves are refused during planning...
    assert agent._is_plan_blocked_leaf(_leaf("exec_shell", "exec_shell")) is True
    assert agent._is_plan_blocked_leaf(_leaf("system_file", "write_file")) is True
    assert agent._is_plan_blocked_leaf(_leaf("scheduler", "schedule_create")) is True
    # ...while read-only leaves (research) stay allowed.
    assert agent._is_plan_blocked_leaf(_leaf("system_file", "read_file")) is False
    assert agent._is_plan_blocked_leaf(_leaf("system_file", "grep_files")) is False
    assert agent._is_plan_blocked_leaf(_leaf("exec_shell", "exec_shell_output")) is False


def test_execute_phase_does_not_block(monkeypatch):
    agent = _build(monkeypatch, mode="plan", plan_phase="execute")
    assert agent._is_plan_blocked_leaf(_leaf("exec_shell", "exec_shell")) is False
    assert agent._is_plan_blocked_leaf(_leaf("system_file", "write_file")) is False


def test_reasoning_and_instant_never_block(monkeypatch):
    for mode in ("reasoning", "instant"):
        agent = _build(monkeypatch, mode=mode)
        assert agent._is_plan_blocked_leaf(_leaf("exec_shell", "exec_shell")) is False


# ── event-run storm prevention (dispatch-time block) ───────────────────────

def test_event_run_blocks_registration_leaves(monkeypatch):
    agent = _build(monkeypatch, mode="reasoning", event_run=True)
    # Event-CREATION leaves are refused inside an event run...
    assert agent._is_event_blocked_leaf(_leaf("scheduler", "schedule_create")) is True
    assert agent._is_event_blocked_leaf(_leaf("system_file", "register_file_watcher")) is True
    # ...but DE-registration leaves stay allowed (they can't storm).
    assert agent._is_event_blocked_leaf(_leaf("scheduler", "schedule_cancel")) is False
    assert agent._is_event_blocked_leaf(_leaf("system_file", "delete_file_watcher")) is False


def test_non_event_run_allows_registration_leaves(monkeypatch):
    agent = _build(monkeypatch, mode="reasoning", event_run=False)
    assert agent._is_event_blocked_leaf(_leaf("scheduler", "schedule_create")) is False
    assert agent._is_event_blocked_leaf(_leaf("system_file", "register_file_watcher")) is False


# ── per-turn plan marker in the volatile input ─────────────────────────────

def test_render_input_carries_plan_marker(monkeypatch):
    planning = _build(monkeypatch, mode="plan", plan_phase="planning")
    planning._current_query = "do the thing"
    rendered = planning._render_input()
    assert rendered.endswith("do the thing")
    assert "PLANNING phase" in rendered
    assert "do NOT execute" in rendered

    execute = _build(monkeypatch, mode="plan", plan_phase="execute")
    execute._current_query = "go"
    r2 = execute._render_input()
    assert "EXECUTION phase" in r2 and r2.endswith("go")


def test_render_input_unchanged_for_reasoning_and_instant(monkeypatch):
    for mode in ("reasoning", "instant"):
        agent = _build(monkeypatch, mode=mode)
        agent._current_query = "hello world"
        assert agent._render_input() == "hello world"


# ── event-run schema hiding (registration tools ABSENT from the tools block) ──
#
# Dispatch-time blocking (above) is the backstop; these assert the event-run
# conversation is not even OFFERED the three registration entry points.

from app.tools.base import FunctionSpec, ToolType, make_leaf_name  # noqa: E402


class _FakeLeafTool:
    """A built-in group exposing named leaves via ``leaf_function_specs``."""

    tool_type = ToolType.BUILTIN

    def __init__(self, tool_id, leaf_names):
        self.tool_id = tool_id
        self._leaf_names = leaf_names

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        return [
            FunctionSpec(
                name=make_leaf_name(self.tool_id, leaf),
                leaf_name=leaf,
                schema={"type": "function", "function": {"name": make_leaf_name(self.tool_id, leaf)}},
            )
            for leaf in self._leaf_names
        ]


class _FakeSkillTool:
    """A skill tool declaring one or more subscribable events."""

    tool_type = ToolType.SKILL

    def __init__(self, tool_id, event_names):
        self.tool_id = tool_id
        self.description = f"{tool_id} skill."
        self.info = SimpleNamespace(
            metadata={"events": {"event_type": [{"name": n} for n in event_names]}}
        )


def _build_dispatch_agent(monkeypatch, tools, *, event_run, loaded_skill_ids=()):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    agent = ra.ReasoningAgent(
        llm=llm, registry=_FakeRegistry(tools), profile="default", context_id="ctx",
        mode="reasoning", event_run=event_run,
    )
    # Attributes normally seeded during run(), not __init__.
    agent._current_query = ""
    agent._loaded_skill_ids = set(loaded_skill_ids)
    return agent


def _leaf_tools():
    return [
        _FakeLeafTool("scheduler", ["schedule_create", "schedule_cancel"]),
        _FakeLeafTool("system_file", ["register_file_watcher", "read_file"]),
    ]


def test_event_run_hides_registration_leaves_from_schema(monkeypatch):
    agent = _build_dispatch_agent(monkeypatch, _leaf_tools(), event_run=True)
    specs, dispatch = agent._build_tools_and_dispatch()
    names = {s["function"]["name"] for s in specs}
    sc = make_leaf_name("scheduler", "schedule_create")
    rfw = make_leaf_name("system_file", "register_file_watcher")
    # Registration leaves are NOT offered to the model...
    assert sc not in names
    assert rfw not in names
    # ...but stay in the dispatch map as a backstop for replayed/echoed calls.
    assert sc in dispatch and rfw in dispatch
    # Non-registration leaves (de-register, read) remain visible.
    assert make_leaf_name("scheduler", "schedule_cancel") in names
    assert make_leaf_name("system_file", "read_file") in names


def test_chat_run_offers_registration_leaves(monkeypatch):
    agent = _build_dispatch_agent(monkeypatch, _leaf_tools(), event_run=False)
    specs, _ = agent._build_tools_and_dispatch()
    names = {s["function"]["name"] for s in specs}
    assert make_leaf_name("scheduler", "schedule_create") in names
    assert make_leaf_name("system_file", "register_file_watcher") in names


def test_event_run_drops_skill_subscribe(monkeypatch):
    # Not-loaded event skill on an event run → exposes `request` only, no subscribe.
    tools = [_FakeSkillTool("mailskill", ["new_email"])]
    agent = _build_dispatch_agent(monkeypatch, tools, event_run=True)
    specs, _ = agent._build_tools_and_dispatch()
    skill_spec = next(s for s in specs if s["function"]["name"] == "mailskill")
    props = skill_spec["function"]["parameters"]["properties"]
    assert "subscribe" not in props
    assert ra.SKILL_REQUEST_ARG in props


def test_event_run_drops_loaded_event_skill_stub(monkeypatch):
    # A LOADED event skill would expose only `subscribe`; hiding it drops the whole
    # stub, but the dispatch entry stays so a re-call short-circuits gracefully.
    tools = [_FakeSkillTool("mailskill", ["new_email"])]
    agent = _build_dispatch_agent(
        monkeypatch, tools, event_run=True, loaded_skill_ids=["mailskill"]
    )
    specs, dispatch = agent._build_tools_and_dispatch()
    assert not any(s["function"]["name"] == "mailskill" for s in specs)
    assert dispatch["mailskill"] == ("skill", tools[0], None)


def test_chat_run_keeps_skill_subscribe(monkeypatch):
    tools = [_FakeSkillTool("mailskill", ["new_email"])]
    agent = _build_dispatch_agent(monkeypatch, tools, event_run=False)
    specs, _ = agent._build_tools_and_dispatch()
    skill_spec = next(s for s in specs if s["function"]["name"] == "mailskill")
    assert "subscribe" in skill_spec["function"]["parameters"]["properties"]
