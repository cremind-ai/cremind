"""What an INTERNAL MAINTENANCE turn is allowed to be.

A maintenance turn is a nested ``agent.run`` the system starts on a
conversation's behalf to keep the conversation healthy — today only the
compaction fold. It borrows the conversation's history, ``context_id`` and
profile, but it represents nobody: nothing streams it, and the caller keeps only
its usage records.

That distinction has to be carried explicitly, because every OTHER identity
signal is absent on a fold. ``message_origin`` is not passed, so a fold on a
group seat has ``_group_chat`` False while ``has_group_membership`` is true by
construction — and the seat gate ("inside a seat the final answer already IS the
post") therefore missed it and left the fold holding ``send_group_message``, a
second mouth able to post into the room with
``originated_from_shadow_turn=False``. Hence a flag rather than another
inference.

The other half of the identity — that a maintenance turn takes nothing from the
inboxes belonging to the turn it runs inside — is pinned in test_compaction.py,
where the real nested path is driven.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _tool(tool_id):
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


# Everything a maintenance turn must not be handed, plus the one tool the fold
# exists to call and one ordinary tool, so "withheld" can be told apart from
# "the list was emptied".
_TOOLS = [
    "send_group_message", "send_notification", "send_channel_message",
    "compact_conversation", "exec_shell",
]

_SPEAKS_TO_SOMEONE_ELSE = (
    "send_group_message", "send_notification", "send_channel_message",
)


def _build(monkeypatch, *, maintenance: bool):
    import app.channels.registry as channel_registry
    import app.groups.index as group_index

    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    # The profile that makes all three tools reachable: in a group, with a
    # notification channel and a live channel of some mode.
    monkeypatch.setattr(channel_registry, "has_any_channel", lambda p: True)
    monkeypatch.setattr(channel_registry, "has_notification_channel", lambda p: True)
    monkeypatch.setattr(group_index, "has_group_membership", lambda p: True)
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    return ra.ReasoningAgent(
        llm=llm, registry=_FakeRegistry([_tool(t) for t in _TOOLS]),
        profile="dog", context_id="ctx", maintenance=maintenance,
    )


def test_a_maintenance_turn_cannot_speak_to_anyone(monkeypatch):
    """Nothing streams a fold, so anything it said to a room or a channel would
    be said on the user's behalf with nobody able to see or correct it."""
    agent = _build(monkeypatch, maintenance=True)

    for tool_id in _SPEAKS_TO_SOMEONE_ELSE:
        assert tool_id not in agent._tools_by_id
    # ...but it keeps the tool it exists to call, and the rest of the run's tools.
    assert "compact_conversation" in agent._tools_by_id
    assert "exec_shell" in agent._tools_by_id


def test_an_ordinary_turn_is_untouched(monkeypatch):
    """The gate must cost a real turn nothing — same profile, same registry, and
    every tool still on the table."""
    agent = _build(monkeypatch, maintenance=False)

    assert set(agent._tools_by_id) == set(_TOOLS)
    assert agent._maintenance is False


def test_maintenance_is_off_unless_asked_for(monkeypatch):
    """Every caller that does not know about this concept keeps today's
    behaviour, which is what makes the flag safe to add mid-stack."""
    llm = SimpleNamespace(provider_name="fake", model_name="fake-model")
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)
    agent = ra.ReasoningAgent(
        llm=llm, registry=_FakeRegistry([]), profile="dog", context_id="ctx",
    )
    assert agent._maintenance is False


# ── the flag reaches the reasoning agent ──────────────────────────────────────


def test_cremind_agent_forwards_the_flag(monkeypatch):
    """``CremindAgent.run`` is the only seam between the fold and the reasoning
    agent; a flag that stopped here would leave the tool gate dead code."""
    from app.agent import agent as agent_mod

    seen: list = []

    class _Recorder:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def run(self, query, history):
            if False:  # pragma: no cover - never yields; shape only
                yield None

    import app.config.settings as settings_mod

    monkeypatch.setattr(agent_mod, "ReasoningAgent", _Recorder)
    monkeypatch.setattr(
        settings_mod.BaseConfig, "is_embedding_enabled", staticmethod(lambda: False),
    )

    mgr = SimpleNamespace(
        config_storage=SimpleNamespace(is_setup_complete=lambda: True),
        create_llm_for_model=lambda profile=None: SimpleNamespace(
            provider_name="fake", model_name="fake-model",
        ),
    )
    cremind = agent_mod.CremindAgent(
        registry=_FakeRegistry([]), embedding=None, model_group_mgr=mgr,
    )

    async def _drive(**kwargs):
        async for _ in cremind.run(
            query="q", task_history=[], context_id="ctx", profile="dog", **kwargs,
        ):
            pass

    asyncio.run(_drive(maintenance=True))
    asyncio.run(_drive())

    assert [k["maintenance"] for k in seen] == [True, False]
