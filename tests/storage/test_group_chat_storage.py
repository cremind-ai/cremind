"""GroupChatStorage: the three group-chat tables as the fan-out sees them.

Most of this is ordinary CRUD, but three behaviours are the reason the class
exists at all and are pinned hardest here:

* a duplicate post is refused with ``None`` rather than an exception, because
  the caller reads ``None`` as "somebody already said this" and skips the
  fan-out — an exception there would abort the whole delivery;
* ``ordering`` is per group and dense, because it is the cursor the SSE stream
  and the ``after=`` pagination both ride on;
* hop accounting is a pair of queries over the timeline, not a counter, so a
  turn that dies and is re-run cannot restart a chain of agents answering each
  other.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages",
    "group_chats", "group_chat_members", "group_chat_messages",
)

_PROFILES = ("dog", "cat", "chicken")


def _storage(tmp_path: Path) -> GroupChatStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "groups.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for profile in _PROFILES:
            c.execute(
                text(
                    "INSERT INTO profiles (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, 0, 0)"
                ),
                {"id": f"pid-{profile}", "name": profile},
            )
    return GroupChatStorage(provider)


async def _post(storage: GroupChatStorage, group_id: str, **kw):
    """One timeline row with the fields every post needs filled in."""
    payload = {
        "group_id": group_id,
        "sender_kind": "user",
        "sender_name": "Alexa",
        "content": "hello",
    }
    payload.update(kw)
    return await storage.add_message(**payload)


# ── groups ──────────────────────────────────────────────────────────────────


def test_create_get_and_list(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(
            name="Ops", created_by="dog", members=["dog", "cat", "dog"],
        )
        assert group["name"] == "Ops"
        assert group["created_by"] == "dog"
        # De-duped, and the member rows carry the (still empty) seat pointer.
        assert group["members"] == ["cat", "dog"]
        assert [r["shadow_conversation_id"] for r in group["member_rows"]] == [None, None]
        assert group["settings"] == {}

        fetched = await storage.get_group(group["id"])
        assert fetched == group
        assert await storage.get_group("nope") is None
        assert await storage.get_group("") is None

    asyncio.run(run())


def test_list_filters_by_member_and_carries_the_last_message(tmp_path: Path) -> None:
    """``last_message`` is what the group list renders under each room's name."""
    storage = _storage(tmp_path)

    async def run():
        ops = await storage.create_group(name="Ops", members=["dog", "cat"])
        farm = await storage.create_group(name="Farm", members=["chicken"])
        await _post(storage, ops["id"], content="what time is it?")

        every = {g["id"]: g for g in await storage.list_groups()}
        assert set(every) == {ops["id"], farm["id"]}
        assert every[ops["id"]]["last_message"]["content"] == "what time is it?"
        assert every[ops["id"]]["last_message"]["sender_kind"] == "user"
        # A room nobody has spoken in yet reads as empty, not as missing.
        assert every[farm["id"]]["last_message"] is None

        mine = await storage.list_groups(member="chicken")
        assert [g["id"] for g in mine] == [farm["id"]]
        assert await storage.list_groups(member="nobody") == []

    asyncio.run(run())


def test_find_group_by_id_and_by_case_insensitive_name(tmp_path: Path) -> None:
    """The CLI takes either; an ambiguous name is the caller's problem to
    report, which is why this returns a list rather than picking one."""
    storage = _storage(tmp_path)

    async def run():
        ops = await storage.create_group(name="Ops", members=["dog"])
        await storage.create_group(name="ops", members=["cat"])

        assert [g["id"] for g in await storage.find_group(ops["id"])] == [ops["id"]]
        assert len(await storage.find_group("OPS")) == 2
        assert len(await storage.find_group("  ops  ")) == 2
        assert await storage.find_group("missing") == []
        assert await storage.find_group("") == []
        assert await storage.find_group(None) == []  # type: ignore[arg-type]

    asyncio.run(run())


def test_update_group_replaces_settings_wholesale(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", settings={"max_agent_hops": 2})
        patched = await storage.update_group(group["id"], name="Operations")
        assert patched["name"] == "Operations"
        assert patched["settings"] == {"max_agent_hops": 2}

        replaced = await storage.update_group(group["id"], settings={"web_sender_name": "Lee"})
        assert replaced["settings"] == {"web_sender_name": "Lee"}
        assert await storage.update_group("nope", name="x") is None

    asyncio.run(run())


def test_set_members_reports_what_changed_with_the_seats_to_tear_down(
    tmp_path: Path,
) -> None:
    """The removed rows carry ``shadow_conversation_id`` because the caller has
    to delete those hidden conversations — the membership row is the only place
    that pointer lives."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog", "cat"])
        await storage.set_shadow_conversation(group["id"], "cat", "conv-cat")

        added, removed = await storage.set_members(group["id"], ["dog", "chicken", "dog"])

        assert added == ["chicken"]
        assert [r["profile"] for r in removed] == ["cat"]
        assert removed[0]["shadow_conversation_id"] == "conv-cat"
        assert (await storage.get_group(group["id"]))["members"] == ["chicken", "dog"]

        # Idempotent: setting the same membership again changes nothing.
        assert await storage.set_members(group["id"], ["dog", "chicken"]) == ([], [])

    asyncio.run(run())


def test_set_shadow_conversation_round_trips_and_clears(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        assert (await storage.get_member(group["id"], "dog"))["shadow_conversation_id"] is None

        await storage.set_shadow_conversation(group["id"], "dog", "conv-1")
        assert (await storage.get_member(group["id"], "dog"))["shadow_conversation_id"] == "conv-1"

        await storage.set_shadow_conversation(group["id"], "dog", None)
        assert (await storage.get_member(group["id"], "dog"))["shadow_conversation_id"] is None

        # A profile that is not a member has no seat and is not created one.
        assert await storage.get_member(group["id"], "cat") is None
        await storage.set_shadow_conversation(group["id"], "cat", "conv-2")
        assert await storage.get_member(group["id"], "cat") is None

        memberships = await storage.list_memberships()
        assert [m["profile"] for m in memberships] == ["dog"]

    asyncio.run(run())


def test_delete_group_takes_its_children_with_it(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog", "cat"])
        gid = group["id"]
        await _post(storage, gid)

        assert await storage.delete_group(gid) is True

        assert await storage.get_group(gid) is None
        assert await storage.list_messages(gid) == []
        assert await storage.list_members(gid) == []
        assert await storage.list_memberships() == []
        assert await storage.delete_group(gid) is False

    asyncio.run(run())


# ── messages ────────────────────────────────────────────────────────────────


def test_ordering_is_dense_and_per_group(tmp_path: Path) -> None:
    """It is the cursor the live stream and ``after=`` both ride on, so one
    group's traffic must not advance another's."""
    storage = _storage(tmp_path)

    async def run():
        ops = await storage.create_group(name="Ops", members=["dog"])
        farm = await storage.create_group(name="Farm", members=["cat"])

        for index in range(3):
            row = await _post(storage, ops["id"], content=f"ops {index}")
            assert row["ordering"] == index
        first_farm = await _post(storage, farm["id"], content="farm 0")
        assert first_farm["ordering"] == 0

    asyncio.run(run())


def test_a_duplicate_source_segment_is_refused(tmp_path: Path) -> None:
    """Re-posting an agent's turn is idempotent, so a crash between writing the
    timeline row and stamping the agent's message cannot double-post."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        kw = {
            "sender_kind": "agent", "sender_profile": "dog", "sender_name": "Rex",
            "source_message_id": "msg-1",
        }
        assert await _post(storage, group["id"], segment=0, **kw) is not None
        assert await _post(storage, group["id"], segment=0, **kw) is None
        # A different segment of the same turn is a different post.
        assert await _post(storage, group["id"], segment=1, **kw) is not None
        # No source id at all is never a duplicate.
        assert await _post(storage, group["id"], sender_kind="agent",
                           sender_profile="dog", sender_name="Rex") is not None

    asyncio.run(run())


def test_five_writers_racing_on_one_turn_leave_one_row(tmp_path: Path) -> None:
    """The read-then-insert above is not atomic, so the UNIQUE has to be the
    thing that actually refuses the duplicate.

    The real shape of the race is the boot sweep and the turn-end hook reaching
    the same agent turn at once; five is simply enough concurrency to lose the
    check every time.
    """
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=list(_PROFILES))
        results = await asyncio.gather(*[
            _post(
                storage, group["id"], sender_kind="agent", sender_profile="dog",
                sender_name="Rex", source_message_id="msg-1", segment=0,
            )
            for _ in range(5)
        ])
        assert sum(1 for r in results if r is not None) == 1
        assert len(await storage.list_messages(group["id"])) == 1

    asyncio.run(run())


def test_list_messages_paginates_forward_and_backward(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        for index in range(5):
            await _post(storage, group["id"], content=f"m{index}")

        assert [m["content"] for m in await storage.list_messages(group["id"])] == [
            "m0", "m1", "m2", "m3", "m4",
        ]
        # ``after`` is the SSE cursor: strictly greater, so no frame repeats.
        assert [m["content"] for m in await storage.list_messages(group["id"], after=2)] == [
            "m3", "m4",
        ]
        assert await storage.list_messages(group["id"], after=4) == []
        assert [m["content"] for m in await storage.list_messages(group["id"], limit=2)] == [
            "m0", "m1",
        ]
        # Opening a room takes the NEWEST slice but still reads oldest-first.
        newest = await storage.list_messages(group["id"], limit=2, newest_first=True)
        assert [m["content"] for m in newest] == ["m3", "m4"]

    asyncio.run(run())


def test_get_message_and_find_by_source(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        kw = {"sender_kind": "agent", "sender_profile": "dog", "sender_name": "Rex",
              "source_message_id": "msg-1"}
        second = await _post(storage, group["id"], segment=1, content="answer", **kw)
        first = await _post(storage, group["id"], segment=0, content="ack", **kw)

        assert (await storage.get_message(first["id"]))["content"] == "ack"
        assert await storage.get_message("nope") is None

        # Segments come back in the order they were spoken, not written.
        found = await storage.find_by_source("msg-1")
        assert [r["id"] for r in found] == [first["id"], second["id"]]
        assert await storage.find_by_source("other") == []
        assert await storage.find_by_source("") == []

    asyncio.run(run())


def test_last_user_ordering_is_the_floor_the_hop_count_measures_from(
    tmp_path: Path,
) -> None:
    """Only a PERSON resets the room. A ``system`` notice ("an agent's turn
    failed") is the room talking about itself, and counting it would hand the
    agents a fresh budget for something no human asked for."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog", "cat"])
        gid = group["id"]
        assert await storage.last_user_ordering(gid) == -1

        await _post(storage, gid)                                           # 0
        await _post(storage, gid, sender_kind="agent", sender_profile="dog",
                    sender_name="Rex")                                      # 1
        await _post(storage, gid, sender_kind="system", sender_name="Cremind")  # 2

        assert await storage.last_user_ordering(gid) == 0

        await _post(storage, gid, sender_name="Lee")                        # 3
        assert await storage.last_user_ordering(gid) == 3
        # Scoped to the room.
        other = await storage.create_group(name="Farm", members=["cat"])
        assert await storage.last_user_ordering(other["id"]) == -1

    asyncio.run(run())


def test_max_agent_hop_after_counts_every_agent_including_the_asker(
    tmp_path: Path,
) -> None:
    """An agent that keeps talking with nobody answering is exactly the runaway
    the cap exists to stop, so its own earlier posts count against it."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog", "cat"])
        gid = group["id"]
        assert await storage.max_agent_hop_after(gid, -1) == -1

        await _post(storage, gid, hop=0)                                    # 0
        await _post(storage, gid, sender_kind="agent", sender_profile="dog",
                    sender_name="Rex", hop=5)                               # 1
        await _post(storage, gid, sender_kind="agent", sender_profile="cat",
                    sender_name="Mia", hop=2)                               # 2

        assert await storage.max_agent_hop_after(gid, -1) == 5
        # Strictly after: the floor row itself is excluded.
        assert await storage.max_agent_hop_after(gid, 1) == 2
        assert await storage.max_agent_hop_after(gid, 2) == -1

    asyncio.run(run())


def test_a_human_post_is_not_an_agent_hop(tmp_path: Path) -> None:
    """Only agent rows count, so a person speaking never raises the number the
    cap is compared against."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        gid = group["id"]
        await _post(storage, gid, hop=0)
        await _post(storage, gid, sender_kind="system", sender_name="Cremind", hop=0)

        assert await storage.max_agent_hop_after(gid, -1) == -1

    asyncio.run(run())


def test_count_agent_posts_since_counts_only_agents_in_the_window(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        gid = group["id"]
        await _post(storage, gid)
        for _ in range(3):
            await _post(storage, gid, sender_kind="agent", sender_profile="dog",
                        sender_name="Rex")
        await _post(storage, gid, sender_kind="system", sender_name="Cremind")

        assert await storage.count_agent_posts_since(gid, 0) == 3
        # A window that has not started yet catches nothing.
        assert await storage.count_agent_posts_since(
            gid, time.time() * 1000 + 60_000,
        ) == 0

    asyncio.run(run())


def test_update_delivered_to_merges_without_duplicating(tmp_path: Path) -> None:
    """The fan-out records each member as it goes, so a crash halfway leaves an
    accurate list the boot sweep can finish from."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=list(_PROFILES))
        row = await _post(storage, group["id"], delivered_to=["dog"])

        await storage.update_delivered_to(row["id"], ["cat"])
        await storage.update_delivered_to(row["id"], ["cat", "chicken"])

        assert (await storage.get_message(row["id"]))["delivered_to"] == [
            "dog", "cat", "chicken",
        ]
        # A row that has since been deleted is not an error.
        await storage.update_delivered_to("nope", ["dog"])

    asyncio.run(run())


def test_a_message_dict_carries_everything_the_fan_out_reads_back(
    tmp_path: Path,
) -> None:
    """The row is handed straight to the bus and to the seat metadata, so its
    keys are an interface rather than an implementation detail."""
    storage = _storage(tmp_path)

    async def run():
        group = await storage.create_group(name="Ops", members=["dog"])
        row = await _post(
            storage, group["id"],
            sender_kind="agent", sender_profile="dog", sender_name="Rex",
            sender_identity={"channel_type": "web", "sender_id": "dog"},
            content="on it", hop=2, source_conversation_id="conv-1",
            source_message_id="msg-1", segment=1,
            delivered_to=["dog"], metadata={"quiet": True},
        )

        assert row["group_id"] == group["id"]
        assert row["sender_identity"] == {"channel_type": "web", "sender_id": "dog"}
        assert row["hop"] == 2
        assert row["segment"] == 1
        assert row["metadata"] == {"quiet": True}
        assert isinstance(row["created_at"], float)
        # Absent JSON reads as an empty container, never None.
        plain = await _post(storage, group["id"], content="plain")
        assert plain["sender_identity"] == {}
        assert plain["delivered_to"] == []
        assert plain["metadata"] == {}

    asyncio.run(run())
