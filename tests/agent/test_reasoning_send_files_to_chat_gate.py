"""The reasoning agent offers ``send_files_to_chat`` only where there is a chat.

The tool sends into the chat the conversation IS — a person's private chat on a
channel, or a platform group — so it belongs in exactly those two
conversations, while a channel is live to carry the files. Everywhere else (the
web UI, an automation's hidden run, a Cremind group seat, a maintenance fold)
there is no chat to send into and the tool is withheld, not merely refused, so
the model never reaches for it there.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _grp(tool_id):
    return SimpleNamespace(
        config_name=tool_id, tool_id=tool_id, name=tool_id, hidden=True, skills=[],
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


_PRIVATE = {
    "source": "channel", "channel_type": "telegram", "channel_id": "ch1",
    "channel_name": "Telegram", "sender_id": "u1", "sender_display_name": "Lee",
}
_GROUP = {
    "source": "channel_group", "channel_id": "ch1", "channel_type": "telegram",
    "channel_name": "Telegram", "group_id": "g1", "group_title": "Sales team",
    "members": [], "member_count": 3,
}
_SEAT = {"source": "group_chat", "group_name": "Ops", "self_profile": "default",
         "members": []}


def _build(monkeypatch, *, origin, any_channel=True, maintenance=False):
    import app.channels.registry as reg
    import app.groups.index as gi

    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda *a, **k: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    monkeypatch.setattr(reg, "has_any_channel", lambda profile: any_channel)
    monkeypatch.setattr(reg, "has_notification_channel", lambda profile: False)
    monkeypatch.setattr(gi, "has_group_membership", lambda profile: False)
    llm = SimpleNamespace(provider_name="openai", model_name="gpt-6-astra")
    registry = _FakeRegistry([
        _grp("send_files_to_chat"),
        _grp("send_channel_message"),
    ])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile="default", context_id="ctx",
        message_origin=origin, maintenance=maintenance,
    )


@pytest.mark.parametrize("origin", [_PRIVATE, _GROUP], ids=["private", "group"])
def test_offered_in_a_channel_chat_while_a_channel_is_live(monkeypatch, origin):
    agent = _build(monkeypatch, origin=origin)
    assert "send_files_to_chat" in agent._tools_by_id


@pytest.mark.parametrize("origin", [
    {"source": "web_ui"}, None, _SEAT,
], ids=["web-ui", "automation", "cremind-seat"])
def test_withheld_where_there_is_no_chat_to_send_into(monkeypatch, origin):
    agent = _build(monkeypatch, origin=origin)
    assert "send_files_to_chat" not in agent._tools_by_id


@pytest.mark.parametrize("origin", [_PRIVATE, _GROUP], ids=["private", "group"])
def test_withheld_while_no_channel_is_live(monkeypatch, origin):
    agent = _build(monkeypatch, origin=origin, any_channel=False)
    assert "send_files_to_chat" not in agent._tools_by_id


def test_a_maintenance_turn_never_gets_it(monkeypatch):
    """A compaction fold borrows the conversation's identity but represents
    nobody: it must not be able to post a file anywhere."""
    agent = _build(monkeypatch, origin=_GROUP, maintenance=True)
    assert "send_files_to_chat" not in agent._tools_by_id
    assert "send_files_to_chat" in ra._MAINTENANCE_BLOCKED_TOOLS


def test_the_two_send_tools_are_gated_independently(monkeypatch):
    """Web UI keeps outreach to people but has no chat of its own to send into."""
    agent = _build(monkeypatch, origin={"source": "web_ui"})
    assert "send_channel_message" in agent._tools_by_id
    assert "send_files_to_chat" not in agent._tools_by_id
