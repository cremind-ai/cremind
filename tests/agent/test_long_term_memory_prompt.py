"""Long-term memory is folded into the system prompt as a frozen section.

The reasoning agent snapshots a profile's durable facts ONCE per process (first
turn to need them) and reuses that byte-identical block on every later step and
turn, so a memory write never re-renders the system block and busts the prompt
cache. These tests cover the formatter, both retrieval paths, the disabled/empty
cases, the freeze semantics, and the template wiring.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


# ── construction scaffolding (mirrors test_reasoning_guidance._build) ──────


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


def _mem_cfg(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        long_term_queue_size=20,
        long_term_max_tokens=50,
        long_term_retrieve_limit=10,
    )


def _build(monkeypatch, *, profile="default"):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_agent_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    registry = _FakeRegistry([_FakeTool("reasoning"), _FakeTool("calc")])
    return ra.ReasoningAgent(llm=llm, registry=registry, profile=profile, context_id="ctx")


@pytest.fixture(autouse=True)
def _clear_snapshot():
    """The snapshot cache is process-global; isolate every test."""
    ra._LONG_TERM_MEMORY_SNAPSHOT.clear()
    yield
    ra._LONG_TERM_MEMORY_SNAPSHOT.clear()


# ── formatter ──────────────────────────────────────────────────────────────


def test_format_memory_block_empty_for_no_facts():
    assert ra._format_memory_block([]) == ""
    assert ra._format_memory_block(["", "   ", None]) == ""  # blanks filtered


def test_format_memory_block_is_self_wrapped_and_bulleted():
    block = ra._format_memory_block(["User is Lee", "Prefers dark mode"])
    assert block.startswith("\n") and block.endswith("\n")   # self-wrapped like REASONING_GUIDANCE
    assert "LONG-TERM MEMORY" in block
    assert "- User is Lee" in block
    assert "- Prefers dark mode" in block


# ── retrieval paths ──────────────────────────────────────────────────────


def test_disabled_feature_yields_empty_block(monkeypatch):
    agent = _build(monkeypatch)
    monkeypatch.setattr(ra, "resolve_memory_config", lambda p: _mem_cfg(enabled=False))
    block = asyncio.run(agent._load_long_term_memory_block())
    assert block == ""


def test_db_path_lists_all_facts(monkeypatch):
    agent = _build(monkeypatch)
    monkeypatch.setattr(ra, "resolve_memory_config", lambda p: _mem_cfg(enabled=True))

    # Force the DB path (embedding off).
    import app.agent.memory_vectorstore as mvs
    monkeypatch.setattr(mvs, "vector_long_term_available", lambda agent: False)

    class _FakeStorage:
        async def get_long_term(self, profile):
            return [{"content": "User is Lee"}, {"content": "Works on Cremind"}]

    monkeypatch.setattr("app.storage.get_memory_storage", lambda: _FakeStorage())

    block = asyncio.run(agent._load_long_term_memory_block())
    assert "- User is Lee" in block
    assert "- Works on Cremind" in block


def test_vector_path_lists_all_facts(monkeypatch):
    agent = _build(monkeypatch)
    monkeypatch.setattr(ra, "resolve_memory_config", lambda p: _mem_cfg(enabled=True))

    import app.agent.memory_vectorstore as mvs
    monkeypatch.setattr(mvs, "vector_long_term_available", lambda agent: True)
    monkeypatch.setattr(
        mvs, "list_long_term",
        lambda **kw: [{"content": "Lives in Hanoi"}, {"content": "Uses PowerShell"}],
    )

    block = asyncio.run(agent._load_long_term_memory_block())
    assert "- Lives in Hanoi" in block
    assert "- Uses PowerShell" in block


def test_db_read_failure_degrades_to_empty(monkeypatch):
    agent = _build(monkeypatch)
    monkeypatch.setattr(ra, "resolve_memory_config", lambda p: _mem_cfg(enabled=True))

    import app.agent.memory_vectorstore as mvs
    monkeypatch.setattr(mvs, "vector_long_term_available", lambda agent: False)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.storage.get_memory_storage", _boom)
    # Must not raise -- a snapshot failure can't break the turn.
    assert asyncio.run(agent._load_long_term_memory_block()) == ""


# ── freeze semantics (once per process, per profile) ──────────────────────


def test_snapshot_frozen_across_turns(monkeypatch):
    """Second agent for the same profile reuses the first snapshot even when the
    underlying memory has changed -- the loader runs exactly once per profile."""
    calls = {"n": 0}

    async def fake_loader(self):
        calls["n"] += 1
        return f"BLOCK{calls['n']}"

    monkeypatch.setattr(ra.ReasoningAgent, "_load_long_term_memory_block", fake_loader)

    agent1 = _build(monkeypatch, profile="P")
    asyncio.run(agent1._ensure_long_term_memory_loaded())
    assert agent1._long_term_memory_block == "BLOCK1"
    assert calls["n"] == 1

    # A new turn (fresh agent) for the same profile: memory "changed" (loader would
    # return BLOCK2) but the frozen snapshot is reused -> loader NOT called again.
    agent2 = _build(monkeypatch, profile="P")
    asyncio.run(agent2._ensure_long_term_memory_loaded())
    assert agent2._long_term_memory_block == "BLOCK1"
    assert calls["n"] == 1

    # A different profile loads its own snapshot independently.
    agent3 = _build(monkeypatch, profile="Q")
    asyncio.run(agent3._ensure_long_term_memory_loaded())
    assert agent3._long_term_memory_block == "BLOCK2"
    assert calls["n"] == 2


# ── template wiring ────────────────────────────────────────────────────────


def test_build_instruction_injects_block_without_breaking_layout(monkeypatch):
    agent = _build(monkeypatch)
    agent._long_term_memory_block = ra._format_memory_block(["User prefers dark mode"])
    prompt = agent._build_instruction()

    assert "LONG-TERM MEMORY" in prompt
    assert "- User prefers dark mode" in prompt
    # Placeholder fully substituted, rest of the template intact.
    assert "{long_term_memory}" not in prompt
    assert "You are a capable assistant." in prompt
    assert "PRESERVE THE USER'S LANGUAGE" in prompt


def test_build_instruction_omits_section_when_block_empty(monkeypatch):
    # Class default is "" -> no section, no leaked placeholder, layout preserved.
    agent = _build(monkeypatch)
    prompt = agent._build_instruction()
    assert "LONG-TERM MEMORY" not in prompt
    assert "{long_term_memory}" not in prompt
    assert "Your name: " in prompt
    assert "You are a capable assistant." in prompt
