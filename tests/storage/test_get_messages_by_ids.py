"""``get_messages_by_ids`` — the bulk read behind the group timeline.

The room's timeline decorates a page of posts with the seat messages that
produced them, which is up to a few hundred lookups for one page load. This is
the one query that does it, so what matters is its contract: keyed by id, silent
about ids that match nothing, and the same dicts ``get_message`` returns.

Storage harness mirrors ``tests/storage/test_message_llm_messages.py`` (real
on-disk SQLite, tables from ORM metadata).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages")


def _make_storage(tmp_path: Path) -> ConversationStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "conv.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    store = ConversationStorage(provider)
    store._initialized = True  # tables created above; skip Alembic init
    return store


def test_it_returns_the_asked_for_messages_keyed_by_id(tmp_path: Path) -> None:
    store = _make_storage(tmp_path)

    async def run() -> None:
        first = await store.add_message(
            conversation_id="c1", role="agent", content="one",
            thinking_steps=[{"tool": "shell"}],
        )
        second = await store.add_message(
            conversation_id="c1", role="agent", content="two",
        )
        await store.add_message(conversation_id="c1", role="user", content="three")

        found = await store.get_messages_by_ids([first["id"], second["id"]])

        assert set(found) == {first["id"], second["id"]}
        assert found[first["id"]]["content"] == "one"
        assert found[first["id"]]["thinking_steps"] == [{"tool": "shell"}]
        # The same shape a single read produces — callers use them
        # interchangeably.
        assert found[second["id"]] == await store.get_message(second["id"])

    asyncio.run(run())


def test_an_id_that_matches_nothing_is_simply_absent(tmp_path: Path) -> None:
    """A room post can outlive the seat message behind it (the conversation was
    pruned). The caller decorates what it finds; a missing row is not an error
    and must not cost the rest of the page its steps."""
    store = _make_storage(tmp_path)

    async def run() -> None:
        real = await store.add_message(
            conversation_id="c1", role="agent", content="one",
        )
        found = await store.get_messages_by_ids([real["id"], "gone-forever"])
        assert set(found) == {real["id"]}

    asyncio.run(run())


def test_nothing_to_look_up_costs_no_query(tmp_path: Path) -> None:
    """Empty and falsy ids short-circuit — the common case is a page of human
    posts, which references no seat message at all."""
    store = _make_storage(tmp_path)

    async def run() -> None:
        assert await store.get_messages_by_ids([]) == {}
        assert await store.get_messages_by_ids(["", None]) == {}  # type: ignore[list-item]

    asyncio.run(run())


def test_a_repeated_id_is_asked_for_once(tmp_path: Path) -> None:
    """Every segment of one turn names the same source message, so duplicates
    are the normal input, not a caller mistake."""
    store = _make_storage(tmp_path)

    async def run() -> None:
        row = await store.add_message(
            conversation_id="c1", role="agent", content="one",
        )
        found = await store.get_messages_by_ids([row["id"], row["id"], row["id"]])
        assert list(found) == [row["id"]]

    asyncio.run(run())
