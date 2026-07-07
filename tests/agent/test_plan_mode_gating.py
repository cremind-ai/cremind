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
