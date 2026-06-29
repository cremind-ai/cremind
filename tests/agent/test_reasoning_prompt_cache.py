"""The system prompt must be byte-identical across turns (not just steps), or the
[tools + system] prompt-cache prefix silently misses.

Under native function calling the tool list is passed via the ``tools=`` param
(not rendered into the prompt text), so the system prompt is just persona / OS /
cwd. Loaded-skill SKILL.md no longer lives in the system prompt either — it rides
the loading tool call's result — so loading a skill never mutates the cached
system block. Two things keep it stable: there is no wall clock in it (that lives
in the on-demand ``get_current_time`` tool), and the ``## Memory`` block lives in
the volatile per-turn user input, not the cached system prompt. These tests guard
both.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _make_agent(monkeypatch):
    """Build a ReasoningAgent skeleton with only what the prompt builders read.

    ``__new__`` bypasses the heavy DI ``__init__``; module-level helpers are
    monkeypatched to fixed values.
    """
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")

    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.profile = "default"
    agent.context_id = None  # skips the working-directory-override / get_context branch
    agent._inject_reasoning_guidance = False
    return agent


def test_system_prompt_stable_and_clock_free(monkeypatch):
    agent = _make_agent(monkeypatch)

    first = agent._build_instruction()
    second = agent._build_instruction()

    assert first == second  # byte-identical across steps/turns
    # The wall clock was removed from the cached prefix — it now lives in the
    # on-demand get_current_time tool, not the system prompt.
    assert "Current time" not in first
    # SKILL.md content is never folded into the system prompt anymore (it rides
    # the loading tool call's result), so the cached system block is unaffected
    # by skill loads.
    assert "Skills Instructions" not in first


def test_input_is_query_only_no_memory_injection(monkeypatch):
    """Long-term memory is no longer injected into the prompt (it would bust the
    cache every turn); the model retrieves it via the search_memory tool. So the
    volatile per-turn input is exactly the user's query."""
    agent = _make_agent(monkeypatch)
    agent._current_query = "hello there"

    # Memory is never in the cached system prompt.
    assert "## Memory" not in agent._build_instruction()

    # The volatile input is just the query — no injected memory block.
    rendered = agent._render_input()
    assert rendered == "hello there"
