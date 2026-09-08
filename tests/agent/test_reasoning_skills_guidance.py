"""The reasoning agent renders a "skills" section into the system prompt so the
model knows a skill is an instruction bundle it must LOAD, not an action it can
call — the plan-mode failure this block exists to fix (skills reached the model
as ordinary-looking functions, so it planned around a one-line description it
had never read).

``_build_skills_guidance`` explains what a skill IS and lists the ENABLED skill
tool_ids. It is built from the enabled tool set only — deliberately NOT from
``_loaded_skill_ids`` — so the section is byte-stable within a run and a
mid-run skill load never mutates the cached system prefix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402
from app.tools.base import ToolType  # noqa: E402


def _skill(tool_id, description="Does a thing."):
    """Stand-in for an installed skill as the registry hands it over: the
    ``tool_type`` discriminator is the ONLY thing that puts it in this block."""
    return SimpleNamespace(
        tool_id=tool_id,
        tool_type=ToolType.SKILL,
        description=description,
    )


def _builtin(tool_id, name=None):
    """A non-skill tool. Built-ins have their own guidance block; they must not
    leak into the skills listing."""
    return SimpleNamespace(
        tool_id=tool_id,
        tool_type=ToolType.BUILTIN,
        name=name or tool_id,
        config_name=tool_id,
        hidden=False,
        skills=[],
    )


def test_no_skills_enabled_returns_empty():
    # Only built-in / MCP-ish stubs enabled: nothing to explain, so no section.
    assert ra._build_skills_guidance([]) == ""
    assert ra._build_skills_guidance([_builtin("exec_shell")]) == ""
    assert ra._build_skills_guidance([SimpleNamespace(tool_id="mcp_thing")]) == ""


def test_skills_enabled_explains_what_a_skill_is_and_lists_ids():
    g = ra._build_skills_guidance([_skill("gmail"), _skill("jira")])
    assert "SKILLS — WHAT THEY ARE" in g
    # The two load-bearing corrections: calling a skill does nothing by itself,
    # and its one-line blurb is not the capability.
    assert "performs NO action" in g
    assert "only a summary" in g
    assert "Enabled skills: `gmail`, `jira`." in g


def test_listing_preserves_input_order():
    g = ra._build_skills_guidance([_skill("zalo"), _skill("atlassian"), _skill("gmail")])
    assert "Enabled skills: `zalo`, `atlassian`, `gmail`." in g


def test_non_skill_tools_ignored():
    # A BUILTIN stub sitting beside a skill contributes nothing to the listing.
    g = ra._build_skills_guidance([_builtin("exec_shell"), _skill("gmail")])
    assert "Enabled skills: `gmail`." in g
    assert "exec_shell" not in g


def test_deterministic():
    tools = [_skill("gmail"), _skill("jira")]
    assert ra._build_skills_guidance(tools) == ra._build_skills_guidance(tools)


# ── end-to-end wiring through the agent ────────────────────────────────────


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


class _FakeRegistry:
    def __init__(self, tools) -> None:
        self._tools = tools

    def tools_for_profile(self, profile):
        return list(self._tools)


def _agent(monkeypatch, tools) -> "ra.ReasoningAgent":
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name="openai", model_name="gpt-6-astra")
    return ra.ReasoningAgent(
        llm=llm, registry=_FakeRegistry(tools), profile="default", context_id="ctx"
    )


def test_agent_wires_skills_guidance_into_prompt(monkeypatch):
    # End-to-end: __init__ computes _skills_guidance from the live tools and
    # _build_instruction injects it. openai/gpt-6-astra reasons natively, so the
    # REASONING STEP block is absent and the prompt is byte-identical per step.
    agent = _agent(monkeypatch, [_builtin("exec_shell"), _skill("gmail")])

    first = agent._build_instruction()
    second = agent._build_instruction()
    assert first == second  # byte-identical across steps
    assert "SKILLS — WHAT THEY ARE" in first
    assert "`gmail`" in first


def test_loaded_skills_do_not_change_the_prompt(monkeypatch):
    # PROMPT-CACHE INVARIANT. The block is derived from the ENABLED skill set and
    # must NEVER depend on which skills have been LOADED: _loaded_skill_ids grows
    # on every skill load mid-run, so a dependency here would rewrite the cached
    # system prefix on each load and bust the provider prompt cache for the rest
    # of the conversation.
    agent = _agent(monkeypatch, [_skill("gmail")])
    before = agent._build_instruction()

    agent._loaded_skill_ids = {"gmail"}
    assert agent._build_instruction() == before


def test_profile_without_skills_has_no_section(monkeypatch):
    agent = _agent(monkeypatch, [_builtin("exec_shell")])
    prompt = agent._build_instruction()
    assert "SKILLS — WHAT THEY ARE" not in prompt
    assert "Enabled skills:" not in prompt
