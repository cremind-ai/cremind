"""Per-conversation (and per-room) search-tool selection in storage.

The API layer and the run snapshot both lean on these methods, so the
contract is pinned here:

- a new conversation stores its selection normalized — ``None`` (the default)
  and "all four" are both ``NULL``, a partial list comes back in priority
  order — at version 0;
- ``set_search_tools`` is a compare-and-set: ``ok`` bumps the version, a
  normalized-equal selection is a ``noop`` that keeps it, a stale version is a
  ``conflict`` that writes nothing, a missing row is ``missing`` — and two
  saves racing on one version can never both win;
- none of it moves ``updated_at`` (the sidebar order);
- the cache baseline has its own getter and never rides the sidebar dict;
- ``has_main_model_activity`` sees an agent turn that carries token usage, or a reasoning usage row
  that outlived a cleared history;
- two profiles' conversations never see each other's selection.
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

from app.agent.search_tools import SelectionError  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_PROFILES = ("alice", "bob")
_ALL = ["documentation_search", "cremind_documentation_search", "memory_search", "web_search"]


def _provider(tmp_path: Path) -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "search.db"))
    eng = provider.sync_engine()
    # Every table: a conversation-id rename repoints every FK child there is.
    Base.metadata.create_all(bind=eng)
    with eng.begin() as c:
        for p in _PROFILES:
            c.execute(text(
                "INSERT INTO profiles (id,name,created_at,updated_at) VALUES (:i,:n,0,0)"
            ), {"i": f"id-{p}", "n": p})
    return provider


def _conversations(tmp_path: Path) -> ConversationStorage:
    store = ConversationStorage(_provider(tmp_path))
    store._initialized = True
    return store


def _run(coro):
    return asyncio.run(coro)


async def _arm(store: ConversationStorage) -> None:
    async with store.engine.begin() as conn:
        await store.provider.apply_pragmas(conn)


# ── create ───────────────────────────────────────────────────────────────────


def test_create_stores_the_normalized_selection(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        default = await store.create_conversation("alice")
        assert default["search_tools"] is None and default["search_tools_version"] == 0

        some = await store.create_conversation("alice", search_tools=["web_search", "documentation_search"])
        assert some["search_tools"] == ["documentation_search", "web_search"]
        assert (await store.get_conversation(some["id"]))["search_tools"] == [
            "documentation_search", "web_search",
        ]

        everything = await store.create_conversation("alice", search_tools=list(reversed(_ALL)))
        assert everything["search_tools"] is None  # "all four" IS the default

        none = await store.create_conversation("alice", search_tools=[])
        assert (await store.get_conversation(none["id"]))["search_tools"] == []

        before = len(await store.list_conversations("alice"))
        with pytest.raises(SelectionError):
            await store.create_conversation("alice", search_tools=["nope"])
        assert len(await store.list_conversations("alice")) == before  # nothing written

    _run(run())


def test_generic_dict_carries_the_selection_but_never_the_baseline(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        assert await store.record_search_cache_baseline(
            conv["id"], {"v": 1, "version": 0, "effective": ["web_search"], "fingerprint": "f", "at": 1},
        )
        listed = (await store.list_conversations("alice"))[0]
        assert "search_cache_baseline" not in listed
        assert listed["search_tools"] is None and listed["search_tools_version"] == 0
        row = await store.get_search_tools_row(conv["id"])
        assert row["search_cache_baseline"]["effective"] == ["web_search"]
        assert row["profile"] == "alice" and row["kind"] == "chat"
        assert await store.get_search_tools_row("missing") is None
        assert await store.record_search_cache_baseline("missing", {"effective": []}) is False

    _run(run())


# ── compare-and-set ─────────────────────────────────────────────────────────


def test_cas_ok_noop_conflict_missing(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        cid = conv["id"]

        status, row = await store.set_search_tools(cid, expected_version=0, selection=["web_search"])
        assert status == "ok"
        assert row["search_tools"] == ["web_search"] and row["search_tools_version"] == 1

        # The same selection again (normalized): nothing written, version kept.
        status, row = await store.set_search_tools(cid, expected_version=1, selection=["web_search"])
        assert status == "noop" and row["search_tools_version"] == 1

        # A stale version never writes — even a different selection.
        status, row = await store.set_search_tools(cid, expected_version=0, selection=[])
        assert status == "conflict"
        assert row["search_tools"] == ["web_search"] and row["search_tools_version"] == 1

        # Select-all is Reset: stored as NULL; then Reset again is a no-op.
        status, row = await store.set_search_tools(cid, expected_version=1, selection=_ALL)
        assert status == "ok" and row["search_tools"] is None and row["search_tools_version"] == 2
        status, row = await store.set_search_tools(cid, expected_version=2, selection=None)
        assert status == "noop" and row["search_tools_version"] == 2

        # Explicitly none is a real choice, distinct from the default.
        status, row = await store.set_search_tools(cid, expected_version=2, selection=[])
        assert status == "ok" and row["search_tools"] == [] and row["search_tools_version"] == 3

        assert await store.set_search_tools("missing", expected_version=0, selection=[]) == ("missing", None)
        with pytest.raises(SelectionError):
            await store.set_search_tools(cid, expected_version=3, selection=["web_search", "web_search"])

    _run(run())


def test_the_default_is_sql_null_not_json_null(tmp_path: Path) -> None:
    """``None`` must reach the row as SQL NULL — the same value every
    pre-upgrade row holds — not SQLAlchemy's JSON ``'null'`` text."""
    store = _conversations(tmp_path)
    groups = GroupChatStorage(store.provider)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        await store.set_search_tools(conv["id"], expected_version=0, selection=["web_search"])
        await store.set_search_tools(conv["id"], expected_version=1, selection=_ALL)
        await store.record_search_cache_baseline(conv["id"], {"effective": []})
        await store.record_search_cache_baseline(conv["id"], None)
        group = await groups.create_group(name="Ops", members=["alice"])
        await groups.set_group_search_tools(group["id"], expected_version=0, selection=[])
        await groups.set_group_search_tools(group["id"], expected_version=1, selection=None)
        with store.provider.sync_engine().connect() as c:
            assert tuple(c.execute(text(
                "SELECT search_tools IS NULL, search_cache_baseline IS NULL FROM conversations"
            )).one()) == (1, 1)
            assert c.execute(text("SELECT search_tools IS NULL FROM group_chats")).scalar() == 1

    _run(run())


def test_racing_saves_on_one_version_cannot_both_win(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        results = await asyncio.gather(
            store.set_search_tools(conv["id"], expected_version=0, selection=["web_search"]),
            store.set_search_tools(conv["id"], expected_version=0, selection=["memory_search"]),
        )
        statuses = sorted(status for status, _ in results)
        assert statuses == ["conflict", "ok"]
        winner = next(row for status, row in results if status == "ok")
        final = await store.get_search_tools_row(conv["id"])
        assert final["search_tools_version"] == 1
        assert final["search_tools"] == winner["search_tools"]

    _run(run())


def test_a_save_landing_between_check_and_write_is_a_conflict(tmp_path: Path) -> None:
    """The version guard is in the UPDATE itself, not just the check before it."""
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        cid = conv["id"]
        real = store._read_search_row
        calls = {"n": 0}

        async def read_then_lose_the_race(session, conversation_id):
            row = await real(session, conversation_id)
            calls["n"] += 1
            if calls["n"] == 1:  # another tab saves right after our check read
                with store.provider.sync_engine().begin() as c:
                    c.execute(text(
                        "UPDATE conversations SET search_tools='[]', search_tools_version=1 "
                        "WHERE id=:i"
                    ), {"i": conversation_id})
            return row

        store._read_search_row = read_then_lose_the_race
        status, row = await store.set_search_tools(cid, expected_version=0, selection=["web_search"])
        assert status == "conflict"
        assert row["search_tools"] == [] and row["search_tools_version"] == 1

    _run(run())


def test_nothing_here_moves_updated_at(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        older = await store.create_conversation("alice", title="older")
        time.sleep(0.01)
        newer = await store.create_conversation("alice", title="newer")
        stamp = (await store.get_conversation(older["id"]))["updated_at"]

        await store.set_search_tools(older["id"], expected_version=0, selection=["web_search"])
        await store.record_search_cache_baseline(older["id"], {"effective": ["web_search"]})
        assert (await store.get_conversation(older["id"]))["updated_at"] == stamp
        # The sidebar still lists the newer chat first.
        assert [c["id"] for c in await store.list_conversations("alice")] == [newer["id"], older["id"]]

    _run(run())


def test_two_profiles_keep_their_own_selection(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        a = await store.create_conversation("alice", search_tools=["documentation_search"])
        b = await store.create_conversation("bob")
        await store.set_search_tools(b["id"], expected_version=0, selection=["web_search"])
        ra, rb = await store.get_search_tools_row(a["id"]), await store.get_search_tools_row(b["id"])
        assert (ra["profile"], ra["search_tools"], ra["search_tools_version"]) == (
            "alice", ["documentation_search"], 0,
        )
        assert (rb["profile"], rb["search_tools"], rb["search_tools_version"]) == ("bob", ["web_search"], 1)

    _run(run())


def test_rename_keeps_the_selection(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice", search_tools=["memory_search"])
        await store.set_search_tools(conv["id"], expected_version=0, selection=["web_search"])
        renamed = await store.rename_conversation_id(conv["id"], "my-chat")
        assert renamed["search_tools"] == ["web_search"] and renamed["search_tools_version"] == 1

    _run(run())


def test_stored_junk_reads_tolerantly(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        with store.provider.sync_engine().begin() as c:
            c.execute(text(
                "UPDATE conversations SET search_tools = '[\"web_search\", \"gone_tool\", 7]', "
                "search_cache_baseline = '\"not a baseline\"' WHERE id = :i"
            ), {"i": conv["id"]})
        row = await store.get_search_tools_row(conv["id"])
        assert row["search_tools"] == ["web_search"]
        assert row["search_cache_baseline"] is None

    _run(run())


# ── main-model activity ─────────────────────────────────────────────────────


def test_main_model_activity(tmp_path: Path) -> None:
    store = _conversations(tmp_path)

    async def run():
        await _arm(store)
        conv = await store.create_conversation("alice")
        cid = conv["id"]
        assert await store.has_main_model_activity(cid) is False
        await store.add_message(cid, "user", content="hi")
        assert await store.has_main_model_activity(cid) is False
        # An agent row the model never produced — an event trigger bubble, a
        # "still initializing" refusal — is not evidence of a model call.
        await store.add_message(cid, "agent", content="Vector embedding is still initializing")
        assert await store.has_main_model_activity(cid) is False
        await store.add_message(cid, "agent", content="hello",
                                token_usage={"input_tokens": 12, "output_tokens": 3})
        assert await store.has_main_model_activity(cid) is True

        # Cleared history: the reasoning usage row still says a request ran.
        other = await store.create_conversation("alice")
        with store.provider.sync_engine().begin() as c:
            c.execute(text(
                "INSERT INTO usage_records (id,conversation_id,profile,source_kind,step_index,"
                "input_tokens,cache_read_input_tokens,cache_creation_input_tokens,output_tokens,"
                "created_at) VALUES ('u1',:c,'alice','reasoning',0,1,0,0,1,0)"
            ), {"c": other["id"]})
        assert await store.has_main_model_activity(other["id"]) is True
        # Another profile's activity is not this conversation's.
        bobs = await store.create_conversation("bob")
        assert await store.has_main_model_activity(bobs["id"]) is False

    _run(run())


# ── group rooms ─────────────────────────────────────────────────────────────


def test_group_room_cas(tmp_path: Path) -> None:
    groups = GroupChatStorage(_provider(tmp_path))

    async def run():
        group = await groups.create_group(name="Ops", created_by="alice", members=["alice", "bob"])
        gid = group["id"]
        assert group["search_tools"] is None and group["search_tools_version"] == 0
        stamp = group["updated_at"]

        status, g = await groups.set_group_search_tools(gid, expected_version=0, selection=["web_search"])
        assert status == "ok" and g["search_tools"] == ["web_search"] and g["search_tools_version"] == 1
        assert g["members"] == ["alice", "bob"]
        assert g["updated_at"] == stamp  # a setting, not activity

        status, g = await groups.set_group_search_tools(gid, expected_version=1, selection=["web_search"])
        assert status == "noop" and g["search_tools_version"] == 1
        status, g = await groups.set_group_search_tools(gid, expected_version=0, selection=[])
        assert status == "conflict" and g["search_tools"] == ["web_search"]
        status, g = await groups.set_group_search_tools(gid, expected_version=1, selection=_ALL)
        assert status == "ok" and g["search_tools"] is None and g["search_tools_version"] == 2

        assert await groups.get_group_search_tools(gid) == {
            "id": gid, "search_tools": None, "search_tools_version": 2,
        }
        assert await groups.get_group_search_tools("missing") is None
        assert await groups.set_group_search_tools("missing", expected_version=0, selection=[]) == (
            "missing", None,
        )
        # Another room is untouched.
        other = await groups.create_group(name="Other", members=["bob"])
        assert (await groups.get_group_search_tools(other["id"]))["search_tools_version"] == 0

    _run(run())


def test_group_racing_saves_cannot_both_win(tmp_path: Path) -> None:
    groups = GroupChatStorage(_provider(tmp_path))

    async def run():
        group = await groups.create_group(name="Ops", members=["alice"])
        results = await asyncio.gather(
            groups.set_group_search_tools(group["id"], expected_version=0, selection=["web_search"]),
            groups.set_group_search_tools(group["id"], expected_version=0, selection=[]),
        )
        assert sorted(s for s, _ in results) == ["conflict", "ok"]
        assert (await groups.get_group_search_tools(group["id"]))["search_tools_version"] == 1

    _run(run())


def test_group_save_landing_between_check_and_write_is_a_conflict(tmp_path: Path) -> None:
    groups = GroupChatStorage(_provider(tmp_path))

    async def run():
        group = await groups.create_group(name="Ops", members=["alice"])
        real = groups.get_group
        calls = {"n": 0}

        async def read_then_lose_the_race(group_id):
            got = await real(group_id)
            calls["n"] += 1
            if calls["n"] == 1:
                with groups.provider.sync_engine().begin() as c:
                    c.execute(text(
                        "UPDATE group_chats SET search_tools='[]', search_tools_version=1 WHERE id=:i"
                    ), {"i": group_id})
            return got

        groups.get_group = read_then_lose_the_race
        status, g = await groups.set_group_search_tools(
            group["id"], expected_version=0, selection=["web_search"],
        )
        assert status == "conflict" and g["search_tools"] == [] and g["search_tools_version"] == 1

    _run(run())
