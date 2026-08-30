"""Reconciling the platform's group list against what we track.

The behaviour worth protecting is the asymmetry between the two kinds of group
this finds. An account is normally in a pile of groups before the feature is
ever switched on, and none of those are news — nobody was added to them, and
announcing each one would open with a wall of decisions the operator never asked
to make. A group that shows up LATER is the opposite: somebody added the account
to something while Cremind happened to be down, and that is exactly what the
notification is for.

The first walk therefore records a baseline in silence; only what appears after
it gets asked about.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels.groups.constants import STATE_GROUP_BASELINE
from app.channels.groups.sweep import reconcile_joined_groups
from tests.channels.groups.conftest import RoomAdapter, make_adapter


class _ListingAdapter(RoomAdapter):
    supports_group_listing = True

    async def fetch_joined_groups(self):
        return self.listed


def _adapter(listed, *, enabled: bool = True, state=None):
    adapter = make_adapter(enabled=enabled)
    adapter.__class__ = _ListingAdapter
    adapter.listed = listed
    if state is not None:
        adapter.channel["state"][STATE_GROUP_BASELINE] = state
    return adapter


def _group(chat_id: str, title: str = "Ops room") -> dict:
    return {
        "platform_chat_id": chat_id, "title": title,
        "chat_type": "supergroup", "member_count": 4,
    }


# ── the first walk ────────────────────────────────────────────────────────


def test_the_groups_an_account_was_already_in_are_recorded_silently(
    group_storage, notifications,
):
    adapter = _adapter([_group("-1"), _group("-2")])
    assert asyncio.run(reconcile_joined_groups(adapter)) == 0

    assert notifications == []
    assert group_storage.groups == {}
    assert set(adapter.channel["state"][STATE_GROUP_BASELINE]) == {"-1", "-2"}


def test_the_baseline_is_written_to_the_channel_row(group_storage, notifications):
    """It has to survive a restart. An in-memory baseline would make every boot
    look like a fresh install and re-ask about every group the account is in."""
    adapter = _adapter([_group("-1")])
    asyncio.run(reconcile_joined_groups(adapter))

    stored = adapter.storage.channels_updated[-1]
    assert stored["state"][STATE_GROUP_BASELINE] == ["-1"]


def test_an_account_in_no_groups_still_gets_a_baseline(
    group_storage, notifications,
):
    """"Never walked" and "walked, found nothing" are different: collapsing them
    would make the next group found look pre-existing and go unannounced."""
    adapter = _adapter([])
    asyncio.run(reconcile_joined_groups(adapter))
    assert adapter.channel["state"][STATE_GROUP_BASELINE] == []


# ── walks after the baseline ──────────────────────────────────────────────


def test_a_group_joined_while_we_were_down_is_announced(
    group_storage, notifications,
):
    adapter = _adapter([_group("-1"), _group("-2", "New room")], state=["-1"])
    assert asyncio.run(reconcile_joined_groups(adapter)) == 1

    (group,) = group_storage.groups.values()
    assert group["platform_chat_id"] == "-2"
    assert group["status"] == "pending"
    assert group["discovered_via"] == "sweep"
    (note,) = notifications
    assert note["kind"] == "channel_group_request"


def test_it_is_announced_once_and_not_again(group_storage, notifications):
    """The baseline absorbs whatever was found, so a group waiting for a
    decision is not re-announced every fifteen minutes."""
    adapter = _adapter([_group("-1"), _group("-2")], state=["-1"])

    async def _run():
        await reconcile_joined_groups(adapter)
        await reconcile_joined_groups(adapter)

    asyncio.run(_run())
    assert len(notifications) == 1
    assert len(group_storage.groups) == 1


def test_a_group_we_already_track_is_left_alone(group_storage, notifications):
    """Discovered by its first message, say — the row already exists and the
    operator has already been asked."""
    adapter = _adapter([_group("-1")], state=[])

    async def _run():
        await group_storage.create_group(
            channel_id=adapter.channel_id, profile=adapter.profile,
            platform_chat_id="-1", title="Ops room",
        )
        return await reconcile_joined_groups(adapter)

    assert asyncio.run(_run()) == 0
    assert notifications == []
    assert len(group_storage.groups) == 1


# ── when it must not run at all ───────────────────────────────────────────


def test_a_channel_with_group_chats_off_is_not_walked(
    group_storage, notifications,
):
    adapter = _adapter([_group("-1")], enabled=False)
    assert asyncio.run(reconcile_joined_groups(adapter)) is None
    assert STATE_GROUP_BASELINE not in adapter.channel["state"]


def test_a_platform_that_cannot_list_groups_is_not_walked(
    group_storage, notifications,
):
    adapter = make_adapter()  # supports_group_listing is False by default
    assert asyncio.run(reconcile_joined_groups(adapter)) is None


def test_a_listing_that_fails_changes_nothing(group_storage, notifications):
    """A network blip must not be read as "the account left every group" — and
    must certainly not write an empty baseline over a real one."""
    adapter = _adapter([], state=["-1"])

    async def _boom():
        raise RuntimeError("socket is closed")

    adapter.fetch_joined_groups = _boom
    assert asyncio.run(reconcile_joined_groups(adapter)) is None
    assert adapter.channel["state"][STATE_GROUP_BASELINE] == ["-1"]


def test_a_broken_sweep_never_raises(group_storage, notifications):
    """It runs on a background timer owned by the adapter; an exception here
    would take the timer down and silently stop reconciling."""
    adapter = _adapter([_group("-1")], state=[])

    async def _boom(*_a, **_kw):
        raise RuntimeError("storage is on fire")

    group_storage.list_groups = _boom
    assert asyncio.run(reconcile_joined_groups(adapter)) is None
