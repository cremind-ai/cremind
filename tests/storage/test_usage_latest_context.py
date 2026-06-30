"""Integration: ``compaction.context_usage`` over real storage + provider catalog.

Pins the model-reported metric end to end after the move from a tiktoken transcript
estimate to the model's actual context size vs. its context window:

- ``add_message`` persists ``token_usage.context_tokens`` plus the stamped
  ``provider``/``model`` metadata;
- the new ``get_latest_agent_message`` reads them back;
- ``context_usage`` reports ``current`` = that reported context size, ``context_window``
  = the catalog value for the stamped model, and ``threshold`` =
  ``compact_threshold_percent / 100 * context_window`` (no monkeypatching — real catalog
  lookup and real config defaults).

Storage harness mirrors ``tests/agent/test_compaction.py`` (real on-disk SQLite,
tables from ORM metadata).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("tiktoken")
pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 — registers tables on Base.metadata
from app.agent import compaction  # noqa: E402
from app.config.user_config import resolve_compaction_config  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages")
_CID = "c1"


def _make_storage(tmp_path: Path) -> ConversationStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "conv.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    from sqlalchemy import text
    now = time.time()
    with engine.begin() as conn:  # FK-enforced sqlite: seed parents first
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p', 'admin', :now, :now)"
        ), {"now": now})
        conn.execute(text(
            "INSERT INTO conversations "
            "(id, profile, title, created_at, updated_at, compaction_watermark) "
            "VALUES (:c, 'admin', 't', :now, :now, -1)"
        ), {"c": _CID, "now": now})
    store = ConversationStorage(provider)
    store._initialized = True
    return store


def test_context_usage_over_real_storage(tmp_path: Path) -> None:
    store = _make_storage(tmp_path)

    async def run() -> None:
        await store.add_message(
            conversation_id=_CID, role="agent", content="ok",
            token_usage={
                "input_tokens": 100, "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 0, "output_tokens": 10,
                "context_tokens": 12345,  # the model-reported prompt size of the turn
            },
            metadata={"provider": "anthropic", "model": "claude-opus-4-7"},
        )

        latest = await store.get_latest_agent_message(_CID)
        assert latest is not None
        assert latest["token_usage"]["context_tokens"] == 12345
        assert latest["metadata"]["model"] == "claude-opus-4-7"

        usage = await compaction.context_usage(
            conversation_id=_CID, profile="admin", conversation_storage=store,
        )
        cfg = resolve_compaction_config("admin")
        assert usage["current_tokens"] == 12345
        assert usage["context_window"] == 1_000_000  # claude-opus-4-7 catalog value
        assert usage["threshold"] == round(cfg.compact_threshold_percent / 100 * 1_000_000)

    asyncio.run(run())
