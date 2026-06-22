"""Tests for the conversation-memory storage layer + extractor parsing.

Locks in the behavior the memory feature depends on:

- short-term FIFO eviction is per-conversation and keeps the newest entries;
- long-term FIFO eviction is per-profile and skips exact-duplicate facts;
- the extraction watermark advances and "un-extracted tokens" counts message
  ``content`` only (reasoning ``thinking_steps`` is never counted);
- the forced ``save_memory`` tool-call result parses into short/long memory.

Harness mirrors ``tests/storage/test_autostart_storage.py``: a real on-disk
SQLite provider with the relevant tables created from the ORM metadata, so the
columns match production exactly. Async storage methods follow the repo's
``asyncio.run`` test convention (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.agent.memory_extractor import _parse_save_memory
from app.agent.memory_runner import build_memory_block
from app.config.user_config import MemoryConfig
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.memory_storage import MemoryStorage

_TABLES = (
    "profiles",
    "channels",
    "conversations",
    "messages",
    "short_term_memories",
    "long_term_memories",
)


def _make_store(tmp_path: Path) -> MemoryStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "memory.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    return MemoryStorage(provider)


def _seed_conversation(store: MemoryStorage, *, profile="admin", conv="c1") -> None:
    from sqlalchemy import text

    now = time.time()
    with store.provider.sync_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at, skill_mode) "
            "VALUES ('p', :profile, :now, :now, 'manual')"
        ), {"profile": profile, "now": now})
        conn.execute(text(
            "INSERT INTO conversations (id, profile, title, created_at, updated_at, memory_watermark) "
            "VALUES (:conv, :profile, 't', :now, :now, 0)"
        ), {"conv": conv, "profile": profile, "now": now})


def test_short_term_fifo_eviction_keeps_newest(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_conversation(store)

    async def run():
        for i in range(5):
            await store.add_short_term("c1", "admin", f"note {i}", 5, queue_size=3)
        entries = await store.get_short_term("c1")
        # Cap honored, oldest dropped, newest kept, oldest→newest order.
        assert [e["content"] for e in entries] == ["note 2", "note 3", "note 4"]

    asyncio.run(run())


def test_long_term_dedup_and_fifo(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_conversation(store)

    async def run():
        first = await store.add_long_term("admin", "Name is Lee", 5, "c1", queue_size=3)
        dup = await store.add_long_term("admin", "Name is Lee", 5, "c1", queue_size=3)
        assert first is not None
        assert dup is None  # exact-duplicate content is skipped

        for i in range(4):
            await store.add_long_term("admin", f"fact {i}", 5, "c1", queue_size=3)
        entries = await store.get_long_term("admin")
        assert len(entries) == 3  # capped per-profile
        # "Name is Lee" + facts 0,1 were evicted; newest three remain.
        assert [e["content"] for e in entries] == ["fact 1", "fact 2", "fact 3"]

    asyncio.run(run())


def test_pending_tokens_counts_content_not_reasoning(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_conversation(store)
    from sqlalchemy import text

    now = time.time()
    with store.provider.sync_engine().begin() as conn:
        # Tiny content, huge thinking_steps — only content must be counted.
        conn.execute(text(
            "INSERT INTO messages (id, conversation_id, role, content, thinking_steps, created_at, ordering) "
            "VALUES ('m1', 'c1', 'user', :content, :think, :now, 1)"
        ), {"content": "hello there", "think": '{"x": "' + ("reasoning " * 500) + '"}', "now": now})

    async def run():
        wm, last = await store.get_watermark("c1")
        assert wm == 0 and last is None
        tokens = await store.unextracted_content_tokens("c1", wm)
        # "hello there" is a handful of tokens — nowhere near the 500-word trace.
        assert 0 < tokens < 20

        window = await store.get_messages_after("c1", 0)
        assert len(window) == 1
        assert window[0]["content"] == "hello there"  # thinking excluded

        await store.set_watermark("c1", 1)
        wm2, last2 = await store.get_watermark("c1")
        assert wm2 == 1 and last2 is not None
        assert await store.unextracted_content_tokens("c1", wm2) == 0

    asyncio.run(run())


def test_parse_save_memory_variants() -> None:
    # Dict arguments (Anthropic-style, already parsed).
    parsed = _parse_save_memory({
        "short_term_memory": "  notes  ",
        "long_term_memories": ["a", "  ", "b"],
    })
    assert parsed == {"short_term_memory": "notes", "long_term_memories": ["a", "b"]}

    # JSON-string arguments (OpenAI-style fallback).
    parsed = _parse_save_memory('{"short_term_memory": "x", "long_term_memories": []}')
    assert parsed == {"short_term_memory": "x", "long_term_memories": []}

    # Nothing usable → None.
    assert _parse_save_memory({"short_term_memory": "", "long_term_memories": []}) is None
    assert _parse_save_memory("not json") is None


def test_build_memory_block_timestamps_and_recency_reminder() -> None:
    block = build_memory_block(
        short_term=[
            {"content": "Prefers concise replies.", "created_at": 1750000000.0},
            {"content": "Correct port is 1112.", "created_at": 1750100000.0},
        ],
        long_term=[{"content": "User name is Lee.", "created_at": 1749000000.0}],
    )
    # Each entry carries a recorded-at timestamp.
    assert block.count("[recorded ") == 3
    assert "Prefers concise replies." in block
    # The header reminds the model that newer memory wins.
    assert "MOST RECENT" in block
    assert "most accurate and reliable" in block
    # Entries are listed oldest-first so the newest is last.
    assert block.index("Prefers concise replies.") < block.index("Correct port is 1112.")

    # Empty / blank-only inputs produce no block.
    assert build_memory_block([], []) == ""
    assert build_memory_block([{"content": "  ", "created_at": 1.0}], []) == ""


def test_memory_config_is_frozen_snapshot() -> None:
    cfg = MemoryConfig(
        enabled=True, trigger_token_threshold=100000, short_term_queue_size=10,
        long_term_queue_size=20, short_term_max_tokens=300, long_term_max_tokens=50,
        temperature=0.3, max_tokens=1024, retry=2,
    )
    assert cfg.enabled is True
    assert cfg.trigger_token_threshold == 100000
