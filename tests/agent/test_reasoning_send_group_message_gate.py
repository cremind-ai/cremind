"""``send_group_message`` is offered only where it makes sense.

Two conversation-constant facts gate it, and both matter:

* a profile in no group has nowhere to post;
* inside a group seat the agent's answer is ALREADY its post, so the tool would
  be a second mouth — an agent that used both would say everything twice.

Constant across a run either way, so the ``tools=`` prefix stays byte-stable and
the prompt cache is unaffected.
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


_GROUP_ORIGIN = {
    "source": "group_chat",
    "group_id": "g1",
    "group_name": "Ops",
    "self_profile": "dog",
    "self_name": "Dog",
    "members": [{"profile": "dog", "agent_name": "Dog", "handles": {}}],
    "users": [],
}


def _build(monkeypatch, *, in_a_group, origin=None):
    import app.channels.registry as channel_registry
    import app.groups.index as group_index

    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    monkeypatch.setattr(channel_registry, "has_any_channel", lambda p: False)
    monkeypatch.setattr(channel_registry, "has_notification_channel", lambda p: False)
    monkeypatch.setattr(group_index, "has_group_membership", lambda p: in_a_group)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    registry = _FakeRegistry([_grp("send_group_message"), _grp("reasoning")])
    return ra.ReasoningAgent(
        llm=llm, registry=registry, profile="dog", context_id="ctx",
        message_origin=origin,
    )


def test_present_for_a_member_outside_the_room(monkeypatch):
    agent = _build(monkeypatch, in_a_group=True)
    assert "send_group_message" in agent._tools_by_id


def test_absent_when_the_profile_is_in_no_group(monkeypatch):
    agent = _build(monkeypatch, in_a_group=False)
    assert "send_group_message" not in agent._tools_by_id


def test_absent_inside_the_seat_itself(monkeypatch):
    """Here the final answer is the post; a tool call would duplicate it."""
    agent = _build(monkeypatch, in_a_group=True, origin=_GROUP_ORIGIN)
    assert "send_group_message" not in agent._tools_by_id


def test_an_uninitialized_index_withholds_rather_than_raising(monkeypatch):
    """The CLI and tests have no group index; a tool gate must never fail a run."""
    import app.groups.index as group_index

    group_index.get_group_index().clear()
    agent = _build(monkeypatch, in_a_group=group_index.has_group_membership("dog"))
    assert "send_group_message" not in agent._tools_by_id
