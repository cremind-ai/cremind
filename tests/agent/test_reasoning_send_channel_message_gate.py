"""The reasoning agent withholds ``send_channel_message`` unless the profile has
a live channel.

Same shape as the ``send_notification`` gate, but deliberately wider: messaging
an individual client works on any mode, so a bot-only profile (no notification
channel at all) must still get this tool. The two gates are independent — that
independence is what these tests pin down.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _grp(config_name, tool_id, *, hidden=False, name=None):
    return SimpleNamespace(
        config_name=config_name,
        tool_id=tool_id,
        name=name or config_name,
        hidden=hidden,
        skills=[],
    )


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


def _build_agent(monkeypatch, *, any_channel, notification_channel=False):
    import app.channels.registry as reg

    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    monkeypatch.setattr(reg, "has_any_channel", lambda profile: any_channel)
    monkeypatch.setattr(
        reg, "has_notification_channel", lambda profile: notification_channel,
    )
    llm = SimpleNamespace(provider_name="openai", model_name="o3")
    registry = _FakeRegistry([
        _grp("send_channel_message", "send_channel_message", hidden=True,
             name="Send Channel Message"),
        _grp("send_notification", "send_notification", hidden=True,
             name="Send Notification"),
    ])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile="default", context_id="ctx"
    )


def test_present_when_a_channel_is_live(monkeypatch):
    agent = _build_agent(monkeypatch, any_channel=True)
    assert "send_channel_message" in agent._tools_by_id


def test_absent_when_no_channel(monkeypatch):
    agent = _build_agent(monkeypatch, any_channel=False)
    assert "send_channel_message" not in agent._tools_by_id


def test_bot_only_profile_gets_direct_send_but_not_notifications(monkeypatch):
    """A conversational-only profile can message clients, not broadcast."""
    agent = _build_agent(monkeypatch, any_channel=True, notification_channel=False)
    assert "send_channel_message" in agent._tools_by_id
    assert "send_notification" not in agent._tools_by_id


def test_both_tools_present_with_a_notification_channel(monkeypatch):
    agent = _build_agent(monkeypatch, any_channel=True, notification_channel=True)
    assert "send_channel_message" in agent._tools_by_id
    assert "send_notification" in agent._tools_by_id
