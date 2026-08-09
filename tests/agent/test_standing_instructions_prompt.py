"""A profile's INSTRUCTIONS.md becomes its own system-prompt section.

Persona says who the agent is; standing instructions say what it must do. The
block is re-read per call (like the persona), and an empty/missing file must
leave the prompt byte-identical to one rendered without the feature.
"""

from __future__ import annotations

import re
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


def _build(monkeypatch, *, instructions="", profile="default"):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_agent_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: instructions)
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    registry = _FakeRegistry([_FakeTool("reasoning"), _FakeTool("calc")])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile=profile, context_id="ctx",
    )


# ── formatter ──────────────────────────────────────────────────────────────


def test_format_block_empty_for_blank_text():
    assert ra._format_standing_instructions_block("") == ""
    assert ra._format_standing_instructions_block("   \n\n  ") == ""
    assert ra._format_standing_instructions_block(None) == ""  # type: ignore[arg-type]


def test_format_block_is_self_wrapped_and_labelled():
    block = ra._format_standing_instructions_block("Register new users.")
    assert block.startswith("\n") and block.endswith("\n")
    assert "STANDING INSTRUCTIONS" in block
    assert "Register new users." in block


# ── template wiring ────────────────────────────────────────────────────────


def test_build_instruction_injects_instructions(monkeypatch):
    agent = _build(
        monkeypatch,
        instructions="Check the 'Active-User' sheet for each new user.",
    )
    prompt = agent._build_instruction()

    assert "STANDING INSTRUCTIONS" in prompt
    assert "Check the 'Active-User' sheet for each new user." in prompt
    assert "{standing_instructions}" not in prompt
    # Persona is still its own thing, ahead of the directives.
    assert prompt.index("PERSONA") < prompt.index("STANDING INSTRUCTIONS")
    assert "You are a capable assistant." in prompt
    assert "PRESERVE THE USER'S LANGUAGE" in prompt


def test_build_instruction_omits_section_when_empty(monkeypatch):
    bare = _build(monkeypatch)._build_instruction()
    assert "STANDING INSTRUCTIONS" not in bare
    assert "{standing_instructions}" not in bare
    # Layout unchanged: no extra blank line where the empty block sits.
    assert re.search(r"Your name: .*\n\nYou are a capable assistant\.", bare)


def test_instructions_are_reread_per_call(monkeypatch):
    """Editing INSTRUCTIONS.md takes effect on the next run without a restart."""
    current = {"text": ""}
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_agent_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: current["text"])
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    agent = ra.ReasoningAgent(
        llm=SimpleNamespace(provider_name="fake", model_name="fake-model"),
        registry=_FakeRegistry([_FakeTool("calc")]),
        profile="default", context_id="ctx",
    )

    assert "STANDING INSTRUCTIONS" not in agent._build_instruction()
    current["text"] = "Always sign off with -Cremind."
    assert "Always sign off with -Cremind." in agent._build_instruction()


def test_system_var_tokens_resolve_inside_instructions(monkeypatch):
    """Tokens work in instructions for free — they resolve after .format()."""
    prompt = _build(
        monkeypatch, instructions="You serve profile $CREMIND_PROFILE.",
        profile="admin",
    )._build_instruction()
    assert "You serve profile admin." in prompt
    assert "$CREMIND_PROFILE" not in prompt
