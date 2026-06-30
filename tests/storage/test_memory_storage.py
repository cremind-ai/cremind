"""Tests for the long-term-memory storage layer.

Short-term memory was unified into the conversation's running compaction summary
(see ``app.agent.compaction``), so the storage layer now only owns long-term
memory (the DB path used when vector embedding is off). This locks in:

- long-term FIFO eviction is per-profile and skips exact-duplicate facts.

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
from app.config.user_config import MemoryConfig
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.memory_storage import MemoryStorage

_TABLES = (
    "profiles",
    "channels",
    "conversations",
    "messages",
    "long_term_memories",
)


def _make_store(tmp_path: Path) -> MemoryStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "memory.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    return MemoryStorage(provider)


def _seed_profile(store: MemoryStorage, *, profile="admin") -> None:
    from sqlalchemy import text

    now = time.time()
    with store.provider.sync_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p', :profile, :now, :now)"
        ), {"profile": profile, "now": now})


def test_long_term_dedup_and_fifo(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_profile(store)

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


def test_memory_config_is_frozen_snapshot() -> None:
    cfg = MemoryConfig(
        enabled=True, long_term_queue_size=20, long_term_max_tokens=50,
        long_term_retrieve_limit=10,
    )
    assert cfg.enabled is True
    assert cfg.long_term_queue_size == 20
    assert cfg.long_term_retrieve_limit == 10
