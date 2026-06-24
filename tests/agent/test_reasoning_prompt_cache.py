"""The system prompt must be byte-identical across the steps of a single turn,
or the [tools + system] prompt-cache prefix silently misses every step.

``ReasoningAgent`` stamps ``current_time`` once per turn (in ``run``) instead of
per step precisely so the prefix stays stable. These tests guard that fix:
``_build_instruction`` is deterministic given a fixed ``_prompt_time``, and the
timestamp is genuinely part of the rendered prefix (so per-turn stamping is what
stabilizes it).
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _make_agent(monkeypatch):
    """Build a ReasoningAgent skeleton with only what ``_build_instruction`` reads.

    ``__new__`` bypasses the heavy DI ``__init__``; module-level helpers are
    monkeypatched to fixed values so the only variable is ``_prompt_time``.
    """
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")

    agent = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    agent.profile = "default"
    agent.context_id = None  # skips the working-directory-override / get_context branch
    agent._memory_context = ""
    agent._prompt_time = "2026-06-24T10:00:00"
    agent._build_tools_block = lambda: "TOOLS_BLOCK"
    agent._build_loaded_skills_block = lambda: ""
    agent._active_action_names = lambda: ["a", "b"]
    return agent


def test_system_prompt_stable_within_turn(monkeypatch):
    agent = _make_agent(monkeypatch)

    first = agent._build_instruction()
    second = agent._build_instruction()

    assert first == second  # byte-identical across steps of the same turn
    assert agent._prompt_time in first  # the stamp is embedded in the cached prefix


def test_system_prompt_varies_when_prompt_time_changes(monkeypatch):
    agent = _make_agent(monkeypatch)

    agent._prompt_time = "2026-06-24T10:00:00"
    first = agent._build_instruction()
    agent._prompt_time = "2026-06-24T10:05:00"
    second = agent._build_instruction()

    # current_time lives inside the prefix, so re-stamping changes it — which is
    # exactly why stamping once per turn (not per step) is what keeps it stable.
    assert first != second
