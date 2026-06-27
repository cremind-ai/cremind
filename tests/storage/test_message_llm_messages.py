"""The ``messages.llm_messages`` JSON column round-trips through storage.

``add_message`` persists the native reasoning trace and ``get_messages`` returns it
verbatim; turns without a trace return ``None`` (so the read path falls back to
content-only). Storage harness mirrors ``tests/agent/test_compaction.py`` (real
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


def test_llm_messages_roundtrip(tmp_path: Path) -> None:
    store = _make_storage(tmp_path)
    trace = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "t", "arguments": '{"x": 1}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]

    async def run() -> None:
        await store.add_message(
            conversation_id="conv1", role="agent", content="done", llm_messages=trace,
        )
        await store.add_message(conversation_id="conv1", role="user", content="hi")

        msgs = await store.get_messages("conv1")
        agent_msg = next(m for m in msgs if m["role"] == "agent")
        user_msg = next(m for m in msgs if m["role"] == "user")

        assert agent_msg["llm_messages"] == trace  # verbatim, incl. arguments JSON
        assert user_msg["llm_messages"] is None    # no trace → content-only on replay

    asyncio.run(run())
