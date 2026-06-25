"""The system prompt must be byte-identical across turns (not just steps), or the
[tools + system] prompt-cache prefix silently misses.

Two things now keep it stable: ``current_time`` was removed from the prompt (it
used to mutate the prefix every turn), and the ``## Memory`` block was moved out of
the system prompt into the volatile per-step input (``template_input``, before
``Begin!``) so per-turn memory retrieval never busts the cached prefix. These tests
guard both.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _make_agent(monkeypatch):
    """Build a ReasoningAgent skeleton with only what ``_build_instruction`` reads.

    ``__new__`` bypasses the heavy DI ``__init__``; module-level helpers are
    monkeypatched to fixed values.
    """
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")

    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.profile = "default"
    agent.context_id = None  # skips the working-directory-override / get_context branch
    agent._memory_context = ""
    agent._build_tools_block = lambda: "TOOLS_BLOCK"
    agent._build_loaded_skills_block = lambda: ""
    agent._active_action_names = lambda: ["a", "b"]
    return agent


def test_system_prompt_stable_and_clock_free(monkeypatch):
    agent = _make_agent(monkeypatch)

    first = agent._build_instruction()
    second = agent._build_instruction()

    assert first == second  # byte-identical across steps/turns
    # The wall clock was removed from the cached prefix — it now lives in the
    # on-demand get_current_time tool, not the system prompt.
    assert "Current time" not in first


def test_memory_block_lives_in_input_not_system_prompt(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent._memory_context = "## Memory\nUser name is Lee."

    instruction = agent._build_instruction()
    # Memory must NOT be in the cached system prompt (it changes per turn).
    assert "## Memory" not in instruction

    rendered = agent._render_input("Thought: hello")
    # It lives in the volatile per-step input, before ``Begin!``.
    assert "## Memory" in rendered
    assert rendered.index("## Memory") < rendered.index("Begin!")

    # With no memory, the input has no stray block and still contains Begin!.
    agent._memory_context = ""
    empty = agent._render_input("Thought: hello")
    assert "## Memory" not in empty
    assert "Begin!" in empty
