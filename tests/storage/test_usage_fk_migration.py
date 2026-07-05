"""Migration 20260703_event_runs: usage_records FK CASCADE → SET NULL on SQLite.

Reproduces a pre-feature ``usage_records`` (NOT NULL + CASCADE conversation FK,
no event_run_id) stamped at the prior head, runs ``upgrade head``, and asserts:

- ``conversation_id`` becomes nullable with ``ON DELETE SET NULL``;
- ``event_run_id`` is added; all pre-existing indexes + the message_id FK survive
  the SQLite table rebuild;
- ``event_runs`` + ``conversations.kind`` exist;
- deleting a conversation now KEEPS its usage rows (conversation_id → NULL) while
  messages still CASCADE away — i.e. Usage & Cost keeps counting deleted convs;
- a corrupt orphan usage row is cleaned so the rebuild's FK-on copy succeeds;
- re-running the upgrade is a no-op.

PostgreSQL uses a different (inspector-discovered drop + alter) path in the same
migration; per CLAUDE.md that branch must be exercised manually against a real
PG instance (this suite is SQLite-only).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260627_llm_messages"

_OLD_USAGE = """
CREATE TABLE usage_records (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  conversation_id VARCHAR(128) NOT NULL,
  message_id VARCHAR(36),
  profile VARCHAR(128) NOT NULL,
  provider VARCHAR(64), model VARCHAR(128), model_group VARCHAR(32),
  source_kind VARCHAR(16) NOT NULL DEFAULT 'reasoning',
  tool_id VARCHAR(128), label VARCHAR(256),
  step_index INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  uncached_input_usd FLOAT, cache_read_usd FLOAT, cache_write_usd FLOAT,
  output_usd FLOAT, total_usd FLOAT, rate_snapshot JSON,
  created_at FLOAT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
  FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
)
"""

_USAGE_INDEXES = [
    "ix_usage_records_conversation_id", "ix_usage_records_message_id",
    "ix_usage_records_profile", "ix_usage_records_provider",
    "ix_usage_records_model", "ix_usage_records_source_kind",
    "ix_usage_records_tool_id", "ix_usage_records_total_usd",
    "ix_usage_records_conv_msg", "ix_usage_records_profile_created",
    "ix_usage_records_event_run_id",  # new
]


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text("CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, created_at FLOAT, updated_at FLOAT)"))
        c.execute(text(
            "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
            "title VARCHAR(256), created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
            "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)"
        ))
        c.execute(text(
            "CREATE TABLE messages (id VARCHAR(36) PRIMARY KEY, conversation_id VARCHAR(128) NOT NULL, "
            "role VARCHAR(16), created_at FLOAT NOT NULL, "
            "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)"
        ))
        c.execute(text(_OLD_USAGE))
        for col in ("conversation_id", "message_id", "profile", "provider", "model",
                    "source_kind", "tool_id", "total_usd"):
            c.execute(text(f"CREATE INDEX ix_usage_records_{col} ON usage_records ({col})"))
        c.execute(text("CREATE INDEX ix_usage_records_conv_msg ON usage_records (conversation_id, message_id)"))
        c.execute(text("CREATE INDEX ix_usage_records_profile_created ON usage_records (profile, created_at)"))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        # sample data
        c.execute(text("INSERT INTO profiles VALUES ('pid','p1',0,0)"))
        c.execute(text("INSERT INTO conversations VALUES ('conv1','p1','T',0,0)"))
        c.execute(text("INSERT INTO messages VALUES ('m1','conv1','agent',0)"))
        c.execute(text(
            "INSERT INTO usage_records (id,conversation_id,message_id,profile,source_kind,"
            "input_tokens,output_tokens,total_usd,created_at) "
            "VALUES ('u1','conv1','m1','p1','reasoning',100,50,0.5,0)"
        ))


def _insert_orphan(db_path: str) -> None:
    """A corrupt orphan row (conversation gone) inserted with FKs OFF."""
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute(
        "INSERT INTO usage_records (id,conversation_id,profile,source_kind,input_tokens,created_at) "
        "VALUES ('u_orphan','ghost','p1','reasoning',1,0)"
    )
    raw.commit()
    raw.close()


def test_usage_fk_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    db_path = str(tmp_path / "old.db")
    provider = SqliteDatabaseProvider(db_path)

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    _insert_orphan(db_path)

    mig.upgrade("head")
    mig.upgrade("head")  # idempotent re-run

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        cols = {x["name"]: x for x in insp.get_columns("usage_records")}
        assert cols["conversation_id"]["nullable"] is True
        assert "event_run_id" in cols

        fks = insp.get_foreign_keys("usage_records")
        conv_fk = next(f for f in fks if f["constrained_columns"] == ["conversation_id"])
        assert (conv_fk.get("options") or {}).get("ondelete", "").upper() == "SET NULL"
        msg_fk = next(f for f in fks if f["constrained_columns"] == ["message_id"])
        assert (msg_fk.get("options") or {}).get("ondelete", "").upper() == "SET NULL"

        idx = {i["name"] for i in insp.get_indexes("usage_records")}
        for name in _USAGE_INDEXES:
            assert name in idx, f"missing index {name}"

        tables = set(insp.get_table_names())
        assert "event_runs" in tables
        assert "kind" in {x["name"] for x in insp.get_columns("conversations")}

        # orphan pre-clean removed the corrupt row; the real row survived.
        assert c.execute(text("SELECT count(*) FROM usage_records")).scalar() == 1

    # Deleting the conversation keeps usage (conversation_id → NULL); messages cascade.
    with eng.begin() as c:
        c.execute(text("DELETE FROM conversations WHERE id='conv1'"))
    with eng.connect() as c:
        row = c.execute(text("SELECT conversation_id, input_tokens FROM usage_records WHERE id='u1'")).fetchone()
        assert row is not None, "usage row wrongly cascade-deleted"
        assert row[0] is None, "conversation_id not nulled"
        assert row[1] == 100
        assert c.execute(text("SELECT count(*) FROM messages")).scalar() == 0
