"""Clearing a conversation's messages while keeping the conversation row.

Backs the channels page's per-subscriber "Clear history": the messages go, the
conversation stays (so the sender's next message continues in it), and the usage
rows stay attached to the conversation — which is what lets the per-sender token
summary keep counting across a wipe. Also covers ``rollup_by_conversation``, the
grouped query that feeds that summary.
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
from app.storage.usage_storage import UsageStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "channel_senders", "conversations", "messages",
    "usage_records",
)


def _provider(tmp_path: Path) -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "clear.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))
    return provider


def _stores(tmp_path: Path):
    provider = _provider(tmp_path)
    cs = ConversationStorage(provider)
    cs._initialized = True
    return cs, UsageStorage(provider)


def test_clear_keeps_conversation_and_usage(tmp_path: Path) -> None:
    cs, us = _stores(tmp_path)

    async def run():
        conv = await cs.create_conversation(profile="p1", title="Lee")
        cid = conv["id"]
        m1 = await cs.add_message(cid, "user", content="hello")
        await cs.add_message(cid, "assistant", content="hi there")
        await us.add_usage_records(
            cid, "p1",
            [{"source_kind": "reasoning", "input_tokens": 100, "output_tokens": 20}],
            message_id=m1["id"], event_run_id=None,
        )

        before = await us.conversation_rollup(cid)
        assert before["input_tokens"] == 100

        cleared = await cs.clear_conversation_messages(cid)
        assert cleared == 2

        # Messages gone, conversation row intact.
        assert await cs.get_messages(cid) == []
        survivor = await cs.get_conversation(cid)
        assert survivor is not None
        assert survivor["id"] == cid

        # Usage still attributed to the conversation — the whole point.
        after = await us.conversation_rollup(cid)
        assert after["input_tokens"] == before["input_tokens"]
        assert after["output_tokens"] == before["output_tokens"]

    asyncio.run(run())


def test_clear_detaches_usage_from_deleted_messages(tmp_path: Path) -> None:
    cs, us = _stores(tmp_path)

    async def run():
        conv = await cs.create_conversation(profile="p1", title="Lee")
        cid = conv["id"]
        msg = await cs.add_message(cid, "user", content="hello")
        await us.add_usage_records(
            cid, "p1", [{"source_kind": "reasoning", "input_tokens": 5}],
            message_id=msg["id"], event_run_id=None,
        )
        await cs.clear_conversation_messages(cid)

        rows = await us.per_request_breakdown(cid)
        assert rows, "usage rows must survive the wipe"
        # The message they pointed at is gone, so the back-reference is cleared
        # rather than dangling.
        assert all(r.get("message_id") is None for r in rows)

    asyncio.run(run())


def test_clear_resets_compaction_state(tmp_path: Path) -> None:
    """A stale watermark would exclude the first new messages from the tail."""
    cs, _ = _stores(tmp_path)

    async def run():
        conv = await cs.create_conversation(profile="p1", title="Lee")
        cid = conv["id"]
        await cs.add_message(cid, "user", content="hello")
        await cs.set_compaction_state(cid, summary="earlier chat", watermark=7, ts=123.0)
        assert await cs.get_compaction_state(cid) == ("earlier chat", 7, 123.0)

        await cs.clear_conversation_messages(cid)

        summary, watermark, last_at = await cs.get_compaction_state(cid)
        assert watermark == -1  # fresh-conversation sentinel, not 0
        assert summary is None
        assert last_at is None

    asyncio.run(run())


def test_clear_is_idempotent_and_scoped(tmp_path: Path) -> None:
    cs, _ = _stores(tmp_path)

    async def run():
        a = await cs.create_conversation(profile="p1", title="A")
        b = await cs.create_conversation(profile="p1", title="B")
        await cs.add_message(a["id"], "user", content="in a")
        await cs.add_message(b["id"], "user", content="in b")

        assert await cs.clear_conversation_messages(a["id"]) == 1
        # Second pass finds nothing left, and never raises.
        assert await cs.clear_conversation_messages(a["id"]) == 0
        # The other conversation is untouched.
        assert len(await cs.get_messages(b["id"])) == 1

    asyncio.run(run())


def test_sender_link_survives_clear(tmp_path: Path) -> None:
    """The sender keeps pointing at the same conversation after a wipe."""
    cs, _ = _stores(tmp_path)

    async def run():
        ch = await cs.create_channel(profile="p1", channel_type="telegram")
        conv = await cs.create_conversation(
            profile="p1", title="Lee", channel_id=ch["id"],
        )
        sender = await cs.get_or_create_sender(
            ch["id"], "84986664411", display_name="Lee Nguyen",
        )
        await cs.update_sender(sender["id"], conversation_id=conv["id"])
        await cs.add_message(conv["id"], "user", content="hello")

        await cs.clear_conversation_messages(conv["id"])

        again = await cs.get_or_create_sender(ch["id"], "84986664411")
        assert again["conversation_id"] == conv["id"]
        # And the reverse lookup the agent uses still resolves.
        found = await cs.get_sender_by_conversation(conv["id"])
        assert found is not None
        assert found["sender_id"] == "84986664411"

    asyncio.run(run())


# ── rollup_by_conversation ──────────────────────────────────────────────────


def test_rollup_by_conversation_groups_and_sums(tmp_path: Path) -> None:
    cs, us = _stores(tmp_path)

    async def run():
        a = await cs.create_conversation(profile="p1", title="A")
        b = await cs.create_conversation(profile="p1", title="B")
        await us.add_usage_records(
            a["id"], "p1",
            [{"source_kind": "reasoning", "input_tokens": 10,
              "cache_read_input_tokens": 4, "cache_creation_input_tokens": 1,
              "output_tokens": 5},
             {"source_kind": "tool", "input_tokens": 20, "output_tokens": 2}],
            message_id=None, event_run_id=None,
        )
        await us.add_usage_records(
            b["id"], "p1",
            [{"source_kind": "reasoning", "input_tokens": 7, "output_tokens": 1}],
            message_id=None, event_run_id=None,
        )

        out = await us.rollup_by_conversation([a["id"], b["id"]])
        assert set(out) == {a["id"], b["id"]}
        assert out[a["id"]]["input_tokens"] == 30
        assert out[a["id"]]["output_tokens"] == 7
        assert out[a["id"]]["total_tokens"] == 30 + 4 + 1 + 7
        assert out[a["id"]]["request_count"] >= 0
        # Cost is frozen at write from the model's pricing (unpriced here → 0.0),
        # but the key must always be present for the UI/CLI to format.
        assert isinstance(out[a["id"]]["total_usd"], float)
        assert out[b["id"]]["input_tokens"] == 7

    asyncio.run(run())


def test_rollup_by_conversation_edge_cases(tmp_path: Path) -> None:
    cs, us = _stores(tmp_path)

    async def run():
        assert await us.rollup_by_conversation([]) == {}
        # Conversations with no usage are simply absent (the UI shows "—").
        conv = await cs.create_conversation(profile="p1", title="A")
        assert await us.rollup_by_conversation([conv["id"], "nonexistent"]) == {}

    asyncio.run(run())
