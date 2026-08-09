"""Message origin (Web UI vs which channel/sender) is a system-prompt section.

The reasoning agent renders the origin ONCE per run from conversation-constant
facts, so the block is byte-stable across a run's steps and cannot fragment the
cached system prefix. A run with no origin must produce the prompt we shipped
before the feature existed. These tests cover the formatter, the freeze
semantics, and the template wiring.
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


def _build(monkeypatch, *, message_origin=None, profile="default"):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_agent_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    registry = _FakeRegistry([_FakeTool("reasoning"), _FakeTool("calc")])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile=profile, context_id="ctx",
        message_origin=message_origin,
    )


_CHANNEL_ORIGIN = {
    "source": "channel",
    "channel_id": "ch-1",
    "channel_type": "telegram",
    "channel_name": "Telegram",
    "sender_id": "84986664411",
    "sender_display_name": "Lee Nguyen",
}


# ── formatter ──────────────────────────────────────────────────────────────


def test_format_origin_block_empty_for_no_origin():
    assert ra._format_message_origin_block(None) == ""
    assert ra._format_message_origin_block({}) == ""
    # An unrecognized source renders nothing rather than a half-filled block.
    assert ra._format_message_origin_block({"source": "mystery"}) == ""


def test_format_origin_block_web_ui_is_self_wrapped():
    block = ra._format_message_origin_block({"source": "web_ui"})
    assert block.startswith("\n") and block.endswith("\n")
    assert "MESSAGE SOURCE" in block
    assert "Web UI" in block


def test_format_origin_block_channel_names_channel_and_sender():
    block = ra._format_message_origin_block(_CHANNEL_ORIGIN)
    assert block.startswith("\n") and block.endswith("\n")
    assert "Telegram" in block
    assert "telegram" in block
    assert "ch-1" in block
    assert "Lee Nguyen" in block
    assert "84986664411" in block
    # Tells the model what to do with it.
    assert "WHO is talking" in block


def test_format_origin_block_channel_without_sender_omits_sender_line():
    block = ra._format_message_origin_block(
        {**_CHANNEL_ORIGIN, "sender_id": None, "sender_display_name": None}
    )
    assert "Telegram" in block
    assert "- Sender:" not in block


def test_format_origin_block_falls_back_to_type_when_unnamed():
    block = ra._format_message_origin_block(
        {**_CHANNEL_ORIGIN, "channel_name": None}
    )
    assert "telegram" in block


def test_format_origin_block_uses_sender_id_when_unnamed():
    block = ra._format_message_origin_block(
        {**_CHANNEL_ORIGIN, "sender_display_name": None}
    )
    assert "84986664411" in block


# ── freeze semantics ───────────────────────────────────────────────────────


def test_origin_block_rendered_once_at_construction(monkeypatch):
    agent = _build(monkeypatch, message_origin=_CHANNEL_ORIGIN)
    assert "Lee Nguyen" in agent._message_origin_block
    # Same bytes on every step — this is what keeps the cached prefix intact.
    assert agent._build_instruction() == agent._build_instruction()


def test_origin_defaults_to_empty_for_skeleton_agents():
    # __new__-built skeletons (tests, direct _build_instruction calls) must not
    # trip on a missing attribute.
    skeleton = ra.ReasoningAgent.__new__(ra.ReasoningAgent)
    assert skeleton._message_origin_block == ""


# ── template wiring ────────────────────────────────────────────────────────


def test_build_instruction_injects_channel_origin(monkeypatch):
    agent = _build(monkeypatch, message_origin=_CHANNEL_ORIGIN)
    prompt = agent._build_instruction()

    assert "MESSAGE SOURCE" in prompt
    assert "Lee Nguyen" in prompt
    assert "{message_origin}" not in prompt
    assert "You are a capable assistant." in prompt
    assert "PRESERVE THE USER'S LANGUAGE" in prompt


def test_build_instruction_injects_web_origin(monkeypatch):
    prompt = _build(monkeypatch, message_origin={"source": "web_ui"})._build_instruction()
    assert "Web UI" in prompt
    assert "{message_origin}" not in prompt


def test_build_instruction_without_origin_is_byte_identical(monkeypatch):
    """No origin ⇒ the prompt we rendered before this feature existed."""
    bare = _build(monkeypatch)._build_instruction()
    assert "MESSAGE SOURCE" not in bare
    assert "{message_origin}" not in bare
    assert "Your name: " in bare
    assert "You are a capable assistant." in bare

    # The origin block occupies the same line as long-term memory / standing
    # instructions: with all three empty the layout must not gain blank lines.
    # ($CREMIND_AGENT_NAME is resolved to the real name by this point.)
    assert re.search(r"Your name: .*\n\nYou are a capable assistant\.", bare)
