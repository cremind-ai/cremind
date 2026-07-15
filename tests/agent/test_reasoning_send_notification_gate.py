"""The reasoning agent withholds ``send_notification`` unless the profile has a
live notification-mode channel.

The gate lives in ``ReasoningAgent.__init__`` (mirroring the ``image_understanding``
gate): it lazily imports ``app.channels.registry.has_notification_channel`` and
drops the tool when it returns False. Evaluated once, frozen into ``self._tools``.
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


def _patch_agent(monkeypatch):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)


def _build_agent(monkeypatch, *, has_channel):
    import app.channels.registry as reg

    _patch_agent(monkeypatch)
    monkeypatch.setattr(reg, "has_notification_channel", lambda profile: has_channel)
    llm = SimpleNamespace(provider_name="openai", model_name="o3")
    registry = _FakeRegistry(
        [_grp("send_notification", "send_notification", hidden=True, name="Send Notification")]
    )
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile="default", context_id="ctx"
    )


def test_send_notification_present_when_channel_enabled(monkeypatch):
    agent = _build_agent(monkeypatch, has_channel=True)
    assert "send_notification" in agent._tools_by_id


def test_send_notification_absent_when_no_channel(monkeypatch):
    agent = _build_agent(monkeypatch, has_channel=False)
    assert "send_notification" not in agent._tools_by_id
