"""The reasoning agent gates the ``reasoning`` think-tool (and its system-prompt
guidance) on the active model's native-reasoning capability.

- Non-reasoning model  -> ``reasoning`` tool kept, REASONING STEP block injected.
- Native-reasoning model -> ``reasoning`` tool dropped, no REASONING STEP block.
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


def _build(monkeypatch, provider_name, model_name):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)

    llm = SimpleNamespace(provider_name=provider_name, model_name=model_name)
    registry = _FakeRegistry([_FakeTool("reasoning"), _FakeTool("calc")])
    return ra.ReasoningAgent(llm=llm, registry=registry, profile="default", context_id="ctx")


def test_non_reasoning_model_keeps_tool_and_injects_guidance(monkeypatch):
    # "fake/fake-model" is in no catalog -> treated as non-reasoning.
    agent = _build(monkeypatch, "fake", "fake-model")
    assert "reasoning" in agent._tools_by_id
    assert agent._inject_reasoning_guidance is True
    assert "REASONING STEP" in agent._build_instruction()


def test_reasoning_model_drops_tool_and_omits_guidance(monkeypatch):
    # openai/o3 is flagged supports_reasoning in the catalog.
    agent = _build(monkeypatch, "openai", "o3")
    assert "reasoning" not in agent._tools_by_id
    assert "calc" in agent._tools_by_id  # other tools untouched
    assert agent._inject_reasoning_guidance is False
    assert "REASONING STEP" not in agent._build_instruction()
