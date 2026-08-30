"""The membership cache: who sits in which room, without a database round trip.

Two questions get asked often enough to be worth caching: does this profile sit
in any group at all (the ``send_group_message`` tool gate, on every prompt
build), and which conversation is this profile's seat in that group (every
fan-out). Both would otherwise be a query on a hot path, and the gate in
particular must never be able to fail a run.

It is a cache with a single writer, so the failure worth pinning is a refresh
that half-succeeds: an index left holding some of the old rows and some of the
new would put a member's message into a seat nobody is sitting in.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

import app.groups.index as index_module  # noqa: E402
from app.groups.index import GroupIndex, has_group_membership  # noqa: E402


class _FakeStorage:
    def __init__(self, memberships=()):
        self._memberships = list(memberships)

    async def list_memberships(self):
        return list(self._memberships)


def _loaded(monkeypatch, memberships=()) -> GroupIndex:
    """A GroupIndex refreshed from a fake database."""
    import app.storage as storage_module

    monkeypatch.setattr(
        storage_module, "get_group_chat_storage",
        lambda *a, **k: _FakeStorage(memberships),
    )
    index = GroupIndex()
    asyncio.run(index.refresh())
    return index


# ── membership ──────────────────────────────────────────────────────────────


def test_refresh_builds_the_membership_lookups(monkeypatch) -> None:
    """Both directions are needed: the fan-out asks a group who is in it, and
    the tool gate asks a profile which groups it is in."""
    index = _loaded(
        monkeypatch,
        memberships=[
            {"group_id": "g1", "profile": "dog", "shadow_conversation_id": "c-dog"},
            {"group_id": "g1", "profile": "cat", "shadow_conversation_id": None},
            {"group_id": "g2", "profile": "dog", "shadow_conversation_id": "c-dog2"},
        ],
    )

    assert index.loaded is True

    assert index.members_of("g1") == {"dog", "cat"}
    assert index.members_of("nope") == set()
    assert index.groups_for_profile("dog") == {"g1", "g2"}
    assert index.groups_for_profile("chicken") == set()

    assert index.shadow_conversation("g1", "dog") == "c-dog"
    assert index.shadow_conversation("g1", "cat") is None
    assert index.shadow_conversation("g2", "dog") == "c-dog2"
    assert index.shadow_conversation("nope", "dog") is None


def test_the_seat_pointer_can_be_noted_and_cleared_without_a_refresh(
    monkeypatch,
) -> None:
    """Seats are created lazily during a fan-out; the index has to learn about
    them without a round trip to the database."""
    index = _loaded(monkeypatch, memberships=[
        {"group_id": "g1", "profile": "dog", "shadow_conversation_id": None},
    ])

    index.note_shadow_conversation("g1", "dog", "conv-new")
    assert index.shadow_conversation("g1", "dog") == "conv-new"
    index.note_shadow_conversation("g1", "dog", None)
    assert index.shadow_conversation("g1", "dog") is None


def test_a_database_that_will_not_answer_leaves_the_index_untouched(
    monkeypatch,
) -> None:
    """Half a refresh is worse than none: a group that lost its members would
    look empty, and the fan-out would quietly deliver a message to nobody."""
    class _Broken:
        async def list_memberships(self):
            raise RuntimeError("db down")

    index = _loaded(monkeypatch, memberships=[
        {"group_id": "g1", "profile": "dog", "shadow_conversation_id": "c-dog"},
    ])
    import app.storage as storage_module
    monkeypatch.setattr(storage_module, "get_group_chat_storage", lambda *a, **k: _Broken())

    asyncio.run(index.refresh())

    assert index.members_of("g1") == {"dog"}
    assert index.groups_for_profile("dog") == {"g1"}
    assert index.shadow_conversation("g1", "dog") == "c-dog"


# ── the tool gate ───────────────────────────────────────────────────────────


def test_has_group_membership_is_false_before_anything_is_loaded(
    monkeypatch,
) -> None:
    """It gates a tool, so it must never raise and must never guess yes — the
    same contract as ``channels.registry.has_any_channel``."""
    monkeypatch.setattr(index_module, "_instance", GroupIndex())
    assert has_group_membership("dog") is False

    import app.storage as storage_module
    monkeypatch.setattr(
        storage_module, "get_group_chat_storage",
        lambda *a, **k: _FakeStorage(memberships=[
            {"group_id": "g1", "profile": "dog", "shadow_conversation_id": None},
        ]),
    )
    asyncio.run(index_module.get_group_index().refresh())

    assert has_group_membership("dog") is True
    assert has_group_membership("cat") is False
    assert has_group_membership("") is False


def test_a_broken_index_reads_as_no_membership(monkeypatch) -> None:
    class _Boom:
        def groups_for_profile(self, profile):
            raise RuntimeError("index exploded")

    monkeypatch.setattr(index_module, "get_group_index", lambda: _Boom())
    assert has_group_membership("dog") is False


def test_clear_forgets_everything(monkeypatch) -> None:
    index = _loaded(monkeypatch, memberships=[
        {"group_id": "g1", "profile": "dog", "shadow_conversation_id": "c1"},
    ])

    index.clear()

    assert index.loaded is False
    assert index.members_of("g1") == set()
    assert index.groups_for_profile("dog") == set()
    assert index.shadow_conversation("g1", "dog") is None
