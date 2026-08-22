"""System-prompt guidance for event tasks, and the cache invariant behind it.

The guidance teaches the one thing no tool description can: "do X, wait, then do
Y" is a supported shape, and the way to serve it is to register a one-shot task
and end the turn. It is appended (never templated) and gated on a
conversation-CONSTANT flag, because a prompt that varies per turn would fragment
the cached prefix of every chat.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402
from app.agent.reasoning_agent import (  # noqa: E402
    EVENT_RUN_GUIDANCE,
    EVENT_TASKS_GUIDANCE,
)


# ── construction scaffolding (mirrors tests/agent/test_long_term_memory_prompt) ──


class _FakeRegistry:
    def tools_for_profile(self, profile):
        return []


def _fake_agent_cfg() -> SimpleNamespace:
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


def _skeleton(monkeypatch, *, event_run: bool):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_agent_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    return ra.ReasoningAgent(
        llm=llm, registry=_FakeRegistry(), profile="p1", context_id="ctx",
        event_run=event_run,
    )


def _render(agent) -> str:
    return agent._build_instruction()


def test_chat_conversations_get_the_event_tasks_block(monkeypatch):
    prompt = _render(_skeleton(monkeypatch, event_run=False))
    assert "EVENT TASKS" in prompt
    assert "END YOUR TURN" in prompt
    assert "Never sleep, poll, or re-check" in prompt


def test_event_runs_get_the_run_block_instead(monkeypatch):
    """The two are exact complements — an event run cannot register anything."""
    prompt = _render(_skeleton(monkeypatch, event_run=True))
    assert "AUTOMATED EVENT RUN" in prompt
    assert "EVENT TASKS — WAITING" not in prompt


def test_event_run_guidance_rules_out_registering_tasks():
    assert "one-shot event task" in EVENT_RUN_GUIDANCE
    assert "cannot register anything from here" in EVENT_RUN_GUIDANCE


def test_the_block_names_all_three_ways_to_wait():
    for surface in ("subscribe", "register_file_watcher", "schedule_create"):
        assert surface in EVENT_TASKS_GUIDANCE


def test_the_block_explains_when_NOT_to_use_a_task():
    """Without this the model turns every standing automation into a one-shot."""
    assert "STANDING subscription" in EVENT_TASKS_GUIDANCE
    assert "every future occurrence" in EVENT_TASKS_GUIDANCE


def test_the_block_teaches_both_ways_a_result_comes_back():
    """A result arrives as a turn, or as a notice mid-turn — never silently.

    The model has to know the second shape exists, or it reads a notice as
    noise; and it has to know ignoring one is safe, or it derails a good turn
    to chase an irrelevant result.
    """
    assert "get_event_task_results" in EVENT_TASKS_GUIDANCE
    assert "nothing needs\npolling" in EVENT_TASKS_GUIDANCE.replace("  ", " ")
    assert "the moment your turn ends" in EVENT_TASKS_GUIDANCE


def test_the_prompt_is_byte_stable_across_renders(monkeypatch):
    """Any per-render variation would bust the cached prefix on every turn."""
    agent = _skeleton(monkeypatch, event_run=False)
    assert _render(agent) == _render(agent)


def test_the_two_populations_differ_only_by_their_appended_block(monkeypatch):
    chat = _render(_skeleton(monkeypatch, event_run=False))
    run = _render(_skeleton(monkeypatch, event_run=True))
    assert chat.endswith(EVENT_TASKS_GUIDANCE)
    assert run.endswith(EVENT_RUN_GUIDANCE)
    assert chat[: -len(EVENT_TASKS_GUIDANCE)] == run[: -len(EVENT_RUN_GUIDANCE)]
