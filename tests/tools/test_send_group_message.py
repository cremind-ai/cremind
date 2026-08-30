"""``send_group_message`` — posting into a room from outside it.

The tool itself is thin; what these tests pin is the part a model gets wrong on
its own. Its hop is never 0 (only a person restarts the agent-to-agent chain, or
a scheduled run could keep a room talking to itself indefinitely), an unknown or
ambiguous group name is reported rather than guessed at, and the result says out
loud that replies arrive in the group rather than in this conversation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("a2a")

from app.tools.builtin import _BUILTIN_MODULE_NAMES  # noqa: E402
from app.tools.builtin.send_group_message import (  # noqa: E402
    TOOL_CONFIG,
    SendGroupMessageTool,
)

_GROUP = {
    "id": "g-1",
    "name": "Morning Ops",
    "members": ["dog", "cat", "chicken"],
    "settings": {},
}


class _GroupStore:
    def __init__(self, matches, listed=()) -> None:
        self._matches = matches
        self._listed = list(listed)

    async def find_group(self, reference):
        return list(self._matches)

    async def list_groups(self, *, member=None):
        return list(self._listed)


def _patch(monkeypatch, *, matches=(_GROUP,), listed=(), posted=None, raises=None):
    import app.events.task_result_inbox as inbox
    import app.groups.fanout as fanout
    import app.storage as storage_pkg
    import app.utils.agent_name as agent_name

    calls: list[dict[str, Any]] = []

    async def _post(**kwargs):
        calls.append(kwargs)
        if raises is not None:
            raise raises
        return posted

    monkeypatch.setattr(
        storage_pkg, "get_group_chat_storage",
        lambda *a, **k: _GroupStore(matches, listed),
    )
    monkeypatch.setattr(agent_name, "read_agent_name", lambda p: p.capitalize())
    monkeypatch.setattr(fanout, "post_message", _post)
    monkeypatch.setattr(inbox, "conversation_for_run", lambda run_id: "conv-9")
    return calls


def _run(**arguments):
    return asyncio.run(SendGroupMessageTool().run({
        "_profile": "dog", "group": "Morning Ops", "message": "status?",
        **arguments,
    }))


def test_it_is_hidden_and_registered():
    """Hidden means the prompt catalogue skips it, so the leaf description is
    the model's only briefing — and it is only useful if it is registered."""
    assert TOOL_CONFIG["hidden"] is True
    assert "send_group_message" in _BUILTIN_MODULE_NAMES


def test_the_description_warns_that_replies_land_in_the_group():
    text = SendGroupMessageTool.description
    assert "REPLIES DO NOT COME BACK HERE" in text
    assert "do not wait" in text


def test_a_happy_post_reports_who_will_see_it(monkeypatch):
    calls = _patch(monkeypatch, posted={"id": "m-1"})
    result = _run()
    body = result.structured_content
    assert body["group_id"] == "g-1"
    assert body["message_id"] == "m-1"
    assert body["delivered_to"] == ["cat", "chicken"]
    assert "in the group" in body["note"]
    assert calls[0]["sender_kind"] == "agent"
    assert calls[0]["sender_name"] == "Dog"
    assert calls[0]["content"] == "status?"


def test_the_hop_is_never_zero(monkeypatch):
    """Zero is what a person's message means. An agent that could post at hop 0
    from a scheduled run would restart the chain at will and never stop."""
    calls = _patch(monkeypatch, posted={"id": "m-1"})
    _run()
    assert calls[0]["hop"] == 1
    assert calls[0]["originated_from_shadow_turn"] is False


def test_it_records_the_conversation_it_was_called_from(monkeypatch):
    calls = _patch(monkeypatch, posted={"id": "m-1"})
    _run()
    assert calls[0]["source_conversation_id"] == "conv-9"


def test_an_unknown_group_lists_the_ones_this_profile_is_in(monkeypatch):
    _patch(monkeypatch, matches=(), listed=[{"id": "g-2", "name": "Ops"}])
    body = _run(group="Nope").structured_content
    assert body["error"] == "UnknownGroup"
    assert body["available_groups"] == ["Ops"]


def test_an_ambiguous_name_asks_for_the_id(monkeypatch):
    _patch(monkeypatch, matches=(_GROUP, {**_GROUP, "id": "g-2"}))
    body = _run().structured_content
    assert body["error"] == "AmbiguousGroup"
    assert {c["id"] for c in body["candidates"]} == {"g-1", "g-2"}


def test_a_non_member_is_refused(monkeypatch):
    _patch(monkeypatch, matches=({**_GROUP, "members": ["cat"]},))
    body = _run().structured_content
    assert body["error"] == "NotAMember"


def test_an_empty_message_is_refused(monkeypatch):
    _patch(monkeypatch, posted={"id": "m-1"})
    assert _run(message="   ").structured_content["error"] == "InvalidArgument"


def test_a_refused_post_is_reported_not_swallowed(monkeypatch):
    """``post_message`` returns None for a duplicate or a vanished group; the
    agent must not go on to tell the user it said something."""
    _patch(monkeypatch, posted=None)
    assert _run().structured_content["error"] == "NotPosted"


def test_a_failure_inside_the_fan_out_is_reported(monkeypatch):
    _patch(monkeypatch, raises=RuntimeError("boom"))
    body = _run().structured_content
    assert body["error"] == "PostFailed"
    assert "boom" in body["message"]
