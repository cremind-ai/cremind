"""Deleting a channel client at the storage layer, against a real database.

Two claims are worth checking against real SQL rather than a fake: that the
sender row really goes (and takes their access state with it, so a re-contact
faces the channel's gate from scratch), and that the surrounding rows behave as
the feature's docstrings promise — usage survives with a nulled conversation
link, and long-term facts are removed only when they came from that
conversation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.memory_storage import MemoryStorage  # noqa: E402
from app.storage.usage_storage import UsageStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "channel_senders", "conversations", "messages",
    "usage_records", "long_term_memories",
)


def _stores(tmp_path: Path):
    provider = SqliteDatabaseProvider(str(tmp_path / "forget.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))
        c.execute(text(
            "INSERT INTO channels (id, profile, channel_type, mode, auth_mode, "
            "response_mode, enabled, created_at, updated_at) "
            "VALUES ('ch1','p1','telegram','bot','none','normal',1,0,0)"
        ))
    cs = ConversationStorage(provider)
    cs._initialized = True
    return cs, UsageStorage(provider), MemoryStorage(provider)


def test_delete_sender_removes_the_row_and_its_access_state(tmp_path: Path) -> None:
    cs, _us, _ms = _stores(tmp_path)

    async def run():
        sender = await cs.get_or_create_sender(
            "ch1", "84901234567", display_name="Lee", phone="84901234567",
        )
        await cs.update_sender(
            sender["id"], authenticated=True, pending_otp="123456",
        )
        assert len(await cs.list_senders("ch1")) == 1

        assert await cs.delete_sender(sender["id"]) is True

        # Nothing about the person is left to consult.
        assert await cs.list_senders("ch1") == []
        # A re-contact is a genuine first contact: fresh row, unauthenticated,
        # no phone carried over, no stale OTP.
        again = await cs.get_or_create_sender("ch1", "84901234567")
        assert again["id"] != sender["id"]
        assert again["authenticated"] is False
        assert again["phone"] is None
        assert again["pending_otp"] is None
        assert again["conversation_id"] is None

    asyncio.run(run())


def test_delete_sender_is_idempotent(tmp_path: Path) -> None:
    cs, _us, _ms = _stores(tmp_path)

    async def run():
        sender = await cs.get_or_create_sender("ch1", "s1")
        assert await cs.delete_sender(sender["id"]) is True
        # A retry after a partial failure must not look like a new error.
        assert await cs.delete_sender(sender["id"]) is False

    asyncio.run(run())


def test_delete_sender_leaves_other_clients_untouched(tmp_path: Path) -> None:
    cs, _us, _ms = _stores(tmp_path)

    async def run():
        keep = await cs.get_or_create_sender("ch1", "keep", display_name="Keep")
        drop = await cs.get_or_create_sender("ch1", "drop")
        await cs.delete_sender(drop["id"])
        remaining = await cs.list_senders("ch1")
        assert [s["sender_id"] for s in remaining] == ["keep"]
        assert remaining[0]["id"] == keep["id"]

    asyncio.run(run())


def test_deleting_the_conversation_keeps_the_spend(tmp_path: Path) -> None:
    """The tokens really were spent, so the usage row outlives the client.

    Only survival is asserted here. That the row's ``conversation_id`` is also
    nulled is a schema guarantee (``ON DELETE SET NULL``) already covered by
    tests/storage/test_usage_fk_migration.py; it cannot be observed through this
    fixture, whose ad-hoc engine runs without ``PRAGMA foreign_keys``, so
    asserting it here would test the harness rather than the product.
    """
    cs, us, _ms = _stores(tmp_path)

    async def run():
        conv = await cs.create_conversation(profile="p1", title="Lee", channel_id="ch1")
        cid = conv["id"]
        msg = await cs.add_message(cid, "user", content="hello")
        await us.add_usage_records(
            cid, "p1",
            [{"source_kind": "reasoning", "input_tokens": 100, "output_tokens": 20}],
            message_id=msg["id"], event_run_id=None,
        )

        await cs.clear_conversation_messages(cid)
        await cs.delete_conversation(cid)

        assert await cs.get_conversation(cid) is None
        assert await cs.get_messages(cid) == []
        async with cs.async_session_maker() as session:
            rows = (await session.execute(text(
                "SELECT input_tokens FROM usage_records"
            ))).fetchall()
        assert [r[0] for r in rows] == [100]

    asyncio.run(run())


def test_memory_delete_is_scoped_to_the_source_conversation(tmp_path: Path) -> None:
    cs, _us, ms = _stores(tmp_path)

    async def run():
        theirs = await cs.create_conversation(profile="p1", title="Client", channel_id="ch1")
        mine = await cs.create_conversation(profile="p1", title="Web", channel_id="ch1")
        await ms.add_long_term("p1", "Client prefers mornings", 4, theirs["id"], 20)
        await ms.add_long_term("p1", "Owner lives in Hanoi", 4, mine["id"], 20)
        await ms.add_long_term("p1", "Fact with no source", 4, None, 20)

        removed = await ms.delete_by_source_conversation("p1", theirs["id"])

        assert removed == 1
        kept = sorted(m["content"] for m in await ms.get_long_term("p1"))
        assert kept == ["Fact with no source", "Owner lives in Hanoi"]

    asyncio.run(run())


def test_teardown_sweeps_both_memory_backends(tmp_path: Path, monkeypatch) -> None:
    """Long-term memory lives in the DB or the vector store, never both.

    Which one depends on whether embedding is enabled, and long-term recall is
    filtered by profile alone — so purging only the backend that happens to be
    empty would leave a deleted client's facts to resurface in a stranger's
    conversation. The teardown must ask both and total the result.
    """
    cs, _us, ms = _stores(tmp_path)

    async def run():
        import app.storage as storage_pkg
        import app.reset._senders as senders_mod
        from app.agent import memory_vectorstore

        conv = await cs.create_conversation(profile="p1", title="C", channel_id="ch1")
        await ms.add_long_term("p1", "a db fact", 3, conv["id"], 20)

        asked: list[tuple[str, str]] = []

        def _fake_forget(*, agent, profile, conversation_id):
            asked.append((profile, conversation_id))
            return 3

        # The helper resolves both stores through module-level accessors, so the
        # test substitutes them there rather than injecting.
        monkeypatch.setattr(storage_pkg, "get_memory_storage", lambda: ms)
        monkeypatch.setattr(memory_vectorstore, "forget_conversation", _fake_forget)

        removed = await senders_mod._forget_conversation_memories("p1", conv["id"])

        assert asked == [("p1", conv["id"])]
        assert removed == 1 + 3           # DB row + vector points
        assert await ms.get_long_term("p1") == []

    asyncio.run(run())


def test_memory_sweep_survives_one_backend_failing(tmp_path: Path, monkeypatch) -> None:
    """A failure in one store must not skip the other."""
    cs, _us, ms = _stores(tmp_path)

    async def run():
        import app.storage as storage_pkg
        import app.reset._senders as senders_mod
        from app.agent import memory_vectorstore

        conv = await cs.create_conversation(profile="p1", title="C", channel_id="ch1")
        await ms.add_long_term("p1", "a db fact", 3, conv["id"], 20)

        def _boom(**_kw):
            raise RuntimeError("vector store unreachable")

        monkeypatch.setattr(storage_pkg, "get_memory_storage", lambda: ms)
        monkeypatch.setattr(memory_vectorstore, "forget_conversation", _boom)

        removed = await senders_mod._forget_conversation_memories("p1", conv["id"])

        # The DB row still went, despite the vector store blowing up.
        assert removed == 1
        assert await ms.get_long_term("p1") == []

    asyncio.run(run())


def test_memory_delete_ignores_other_profiles_and_empty_ids(tmp_path: Path) -> None:
    cs, _us, ms = _stores(tmp_path)

    async def run():
        conv = await cs.create_conversation(profile="p1", title="C", channel_id="ch1")
        await ms.add_long_term("p1", "a fact", 2, conv["id"], 20)
        # Right conversation id, wrong profile — must not reach across.
        assert await ms.delete_by_source_conversation("other", conv["id"]) == 0
        assert await ms.delete_by_source_conversation("p1", "") == 0
        assert len(await ms.get_long_term("p1")) == 1

    asyncio.run(run())
