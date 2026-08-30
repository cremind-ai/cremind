"""Resolving a seat conversation into the room the agent is told about.

The branch runs before the channel lookup because a seat lives on the profile's
hidden ``main`` channel: resolved the other way round every group agent would be
told it was chatting with the operator in the Web UI.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

import app.agent.stream_runner as sr  # noqa: E402


class _Store:
    """Just enough conversation storage for the origin resolver."""

    def __init__(self) -> None:
        self.channel_lookups = 0
        self.sender_lookups = 0

    async def get_channel(self, channel_id):
        self.channel_lookups += 1
        return {"id": channel_id, "channel_type": "main"}

    async def get_sender_by_conversation(self, conversation_id):
        self.sender_lookups += 1
        return None


class _GroupStore:
    def __init__(self, group=None) -> None:
        self._group = group

    async def get_group(self, group_id):
        return self._group


_GROUP = {
    "id": "g-1",
    "name": "Morning Ops",
    "settings": {"max_agent_hops": 4},
    "members": ["cat", "dog"],
}


def _patch(monkeypatch, group):
    import app.storage as storage_pkg
    import app.utils.agent_name as agent_name

    monkeypatch.setattr(
        storage_pkg, "get_group_chat_storage", lambda *a, **k: _GroupStore(group),
    )
    monkeypatch.setattr(
        agent_name, "read_agent_name", lambda profile: profile.capitalize(),
    )


def _resolve(store, conv, *, event_run=False):
    return asyncio.run(
        sr._resolve_message_origin(
            store, conv, conv.get("id", "c1"), event_run=event_run,
        )
    )


def _seat(**over):
    return {
        "id": "seat-1",
        "profile": "dog",
        "kind": "group_chat",
        "context_id": "group:g-1:dog",
        "channel_id": "main-1",
        **over,
    }


def test_a_seat_resolves_to_its_room(monkeypatch):
    _patch(monkeypatch, _GROUP)
    store = _Store()
    origin = _resolve(store, _seat())
    assert origin["source"] == "group_chat"
    assert origin["group_id"] == "g-1"
    assert origin["group_name"] == "Morning Ops"
    assert origin["self_profile"] == "dog"
    assert origin["self_name"] == "Dog"
    assert [m["profile"] for m in origin["members"]] == ["cat", "dog"]
    # The room's settings are read through, not re-derived from the defaults.
    assert origin["max_agent_hops"] == 4


def test_it_never_looks_the_seat_up_as_a_channel(monkeypatch):
    """The seat sits on the ``main`` channel; asking about it would answer
    'Web UI' and cost two pointless reads."""
    _patch(monkeypatch, _GROUP)
    store = _Store()
    _resolve(store, _seat())
    assert store.channel_lookups == 0
    assert store.sender_lookups == 0


def test_a_vanished_group_degrades_to_no_origin(monkeypatch):
    _patch(monkeypatch, None)
    assert _resolve(_Store(), _seat()) is None


def test_a_broken_context_id_degrades_to_no_origin(monkeypatch):
    _patch(monkeypatch, _GROUP)
    assert _resolve(_Store(), _seat(context_id="not-a-group")) is None


def test_an_ordinary_conversation_is_unaffected(monkeypatch):
    _patch(monkeypatch, _GROUP)
    store = _Store()
    origin = _resolve(store, {
        "id": "c1", "profile": "dog", "kind": "chat", "channel_id": "main-1",
    })
    assert origin == {"source": "web_ui"}
    assert store.channel_lookups == 1


def test_an_event_run_is_still_none(monkeypatch):
    _patch(monkeypatch, _GROUP)
    assert _resolve(_Store(), _seat(), event_run=True) is None
