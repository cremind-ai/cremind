"""ChannelGroupStorage against a real SQLite database.

Two behaviours here are the reason this class exists rather than being inlined:
discovery losing a race gracefully, and the roster/seen precedence rule. Both
are easy to get subtly wrong in a way no type checker catches and only a real
unique constraint reveals.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 — registers tables on Base.metadata
from sqlalchemy import text  # noqa: E402

from app.channels.groups import constants as group_constants  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.channel_group_storage import ChannelGroupStorage  # noqa: E402

_TABLES = (
    "profiles",
    "channels",
    "conversations",
    "channel_groups",
    "channel_group_members",
)


def _storage(tmp_path: Path) -> ChannelGroupStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "cg.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    now = time.time() * 1000
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id,name,created_at,updated_at) "
            "VALUES ('p','admin',:n,:n)"
        ), {"n": now})
        c.execute(text(
            "INSERT INTO channels (id,profile,channel_type,mode,auth_mode,"
            "response_mode,enabled,created_at,updated_at) VALUES "
            "('ch1','admin','telegram','bot','none','normal',1,:n,:n)"
        ), {"n": now})
        c.execute(text(
            "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
            "VALUES ('conv1','admin','chat','Ops room',:n,:n)"
        ), {"n": now})
    return ChannelGroupStorage(provider)


async def _create(store: ChannelGroupStorage, **overrides):
    kwargs = {
        "channel_id": "ch1", "profile": "admin", "platform_chat_id": "-1001",
        "title": "Ops room", "chat_type": "supergroup",
    }
    kwargs.update(overrides)
    return await store.create_group(**kwargs)


# ── the vocabulary this module spells out for itself ──────────────────────


def test_the_literal_status_words_match_the_canonical_ones():
    """``channel_group_storage`` cannot import ``app.channels`` (that closes a
    cycle through the registry), so it spells these out. This is the pin that
    keeps the two spellings from drifting apart silently."""
    import app.storage.channel_group_storage as mod

    assert mod._STATUS_PENDING == group_constants.STATUS_PENDING
    assert mod._DISCOVERED_VIA_MESSAGE == group_constants.DISCOVERED_VIA_MESSAGE
    assert mod._MEMBER_SOURCE_ROSTER == group_constants.MEMBER_SOURCE_ROSTER
    assert mod._MEMBER_SOURCE_SEEN == group_constants.MEMBER_SOURCE_SEEN


# ── discovery ─────────────────────────────────────────────────────────────


def test_a_new_group_starts_pending(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    group = asyncio.run(_create(store))
    assert group["status"] == "pending"
    assert group["discovered_via"] == "message"
    assert group["title"] == "Ops room"
    assert group["members"] == []


def test_discovering_the_same_chat_twice_returns_the_first_row(
    tmp_path: Path,
) -> None:
    """The unique constraint is the arbiter, and losing to it is a NORMAL
    outcome: two messages from an unknown group can arrive on the same tick, and
    the alternative to losing gracefully is two pending rows and two
    notifications for one group."""
    store = _storage(tmp_path)

    async def _run():
        first = await _create(store)
        await store.update_group(first["id"], status="approved")
        second = await _create(store)
        return first, second

    first, second = asyncio.run(_run())
    assert second["id"] == first["id"]
    # And it does not reset what the operator already decided.
    assert second["status"] == "approved"


def test_the_same_chat_on_a_different_channel_is_a_different_group(
    tmp_path: Path,
) -> None:
    """Two profiles' bots in one Telegram group get a row each, and approve
    independently."""
    store = _storage(tmp_path)
    eng = store.provider.sync_engine()
    now = time.time() * 1000
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id,name,created_at,updated_at) "
            "VALUES ('p2','dog',:n,:n)"
        ), {"n": now})
        c.execute(text(
            "INSERT INTO channels (id,profile,channel_type,mode,auth_mode,"
            "response_mode,enabled,created_at,updated_at) VALUES "
            "('ch2','dog','telegram','bot','none','normal',1,:n,:n)"
        ), {"n": now})

    async def _run():
        a = await _create(store)
        b = await _create(store, channel_id="ch2", profile="dog")
        return a, b

    a, b = asyncio.run(_run())
    assert a["id"] != b["id"]


# ── lookups the hot path depends on ───────────────────────────────────────


def test_a_group_is_found_by_chat_and_by_conversation(tmp_path: Path) -> None:
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        await store.update_group(group["id"], conversation_id="conv1")
        return (
            await store.get_group_by_chat("ch1", "-1001"),
            await store.get_group_by_conversation("conv1"),
            await store.get_group_by_chat("ch1", "-9999"),
        )

    by_chat, by_conv, missing = asyncio.run(_run())
    assert by_chat["platform_chat_id"] == "-1001"
    assert by_conv["id"] == by_chat["id"]
    assert missing is None


def test_listing_can_be_narrowed_to_one_status(tmp_path: Path) -> None:
    store = _storage(tmp_path)

    async def _run():
        pending = await _create(store, platform_chat_id="-1")
        approved = await _create(store, platform_chat_id="-2")
        await store.update_group(approved["id"], status="approved")
        return (
            await store.list_groups("ch1"),
            await store.list_groups("ch1", status="pending"),
        ), pending["id"]

    (everything, only_pending), pending_id = asyncio.run(_run())
    assert len(everything) == 2
    assert [g["id"] for g in only_pending] == [pending_id]


def test_a_patch_leaves_the_fields_it_does_not_name(tmp_path: Path) -> None:
    """``None`` means "not supplied", not "set to null" — a caller changing the
    status must not blank the title on its way past."""
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        return await store.update_group(group["id"], status="approved")

    updated = asyncio.run(_run())
    assert updated["status"] == "approved"
    assert updated["title"] == "Ops room"


# ── members ───────────────────────────────────────────────────────────────


def test_a_roster_write_overrules_what_seeing_somebody_post_knew(
    tmp_path: Path,
) -> None:
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        await store.upsert_member(
            group["id"], member_id="u1", display_name="alexa", source="seen",
        )
        await store.upsert_member(
            group["id"], member_id="u1", display_name="Alexa Nguyen",
            role="admin", source="roster",
        )
        return await store.list_members(group["id"])

    (member,) = asyncio.run(_run())
    assert member["display_name"] == "Alexa Nguyen"
    assert member["role"] == "admin"
    assert member["source"] == "roster"


def test_seeing_somebody_post_never_erases_what_the_roster_gave_them(
    tmp_path: Path,
) -> None:
    """The precedence rule that matters in practice: every message from a named
    member would otherwise blank the name."""
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        await store.upsert_member(
            group["id"], member_id="u1", display_name="Alexa Nguyen",
            role="admin", source="roster",
        )
        await store.upsert_member(
            group["id"], member_id="u1", display_name=None, source="seen",
            count_message=True,
        )
        return await store.list_members(group["id"])

    (member,) = asyncio.run(_run())
    assert member["display_name"] == "Alexa Nguyen"
    assert member["role"] == "admin"
    assert member["source"] == "roster"
    assert member["message_count"] == 1


def test_a_bot_flag_is_only_ever_set(tmp_path: Path) -> None:
    """The platform that says "bot" knows; the ones that say nothing say
    nothing, and must not be read as "not a bot"."""
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        await store.upsert_member(group["id"], member_id="b1", is_bot=True)
        await store.upsert_member(group["id"], member_id="b1", is_bot=False)
        return await store.list_members(group["id"])

    (member,) = asyncio.run(_run())
    assert member["is_bot"] is True


def test_somebody_who_left_is_demoted_not_deleted(tmp_path: Path) -> None:
    """They are still the author of messages in the transcript, and the member
    policy may still name them."""
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        await store.replace_roster(group["id"], [
            {"member_id": "u1", "display_name": "Alexa"},
            {"member_id": "u2", "display_name": "Sam"},
        ])
        await store.replace_roster(group["id"], [
            {"member_id": "u1", "display_name": "Alexa"},
        ])
        return await store.list_members(group["id"])

    members = {m["member_id"]: m for m in asyncio.run(_run())}
    assert members["u1"]["source"] == "roster"
    assert members["u2"]["source"] == "seen"


def test_a_member_upsert_on_a_group_that_is_gone_says_so_quietly(
    tmp_path: Path,
) -> None:
    """A message can arrive while the operator is forgetting the group.

    Arms ``foreign_keys=ON`` first, as ``ConversationStorage.initialize`` does in
    production: the FK is what turns this into an ``IntegrityError`` the storage
    swallows, and without the pragma SQLite would happily write an orphan row.
    """
    store = _storage(tmp_path)

    async def _run():
        async with store.provider.async_engine().begin() as conn:
            await store.provider.apply_pragmas(conn)
        return await store.upsert_member("nope", member_id="u1")

    assert asyncio.run(_run()) is None


# ── teardown ──────────────────────────────────────────────────────────────


def test_forgetting_a_group_takes_its_members_with_it(tmp_path: Path) -> None:
    """Deleted explicitly rather than relying on CASCADE: SQLite only enforces
    that with ``foreign_keys=ON``, and the outcome must not depend on a pragma."""
    store = _storage(tmp_path)

    async def _run():
        group = await _create(store)
        await store.upsert_member(group["id"], member_id="u1")
        deleted = await store.delete_group(group["id"])
        return deleted, await store.get_group(group["id"]), group["id"]

    deleted, found, gid = asyncio.run(_run())
    assert deleted is True
    assert found is None
    with store.provider.sync_engine().connect() as c:
        left = c.execute(text(
            "SELECT COUNT(*) FROM channel_group_members WHERE group_id=:g"
        ), {"g": gid}).scalar()
    assert left == 0
