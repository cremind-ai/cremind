"""Migration 20260930_tags and the journal's commit ordering on PostgreSQL.

The same DDL as ``test_tags_migration.py``, driven through the real Alembic
chain from an EMPTY database to the prior head and then to this revision
(twice), plus the property the head-row lock exists for: two transactions
appending for one profile commit in seq order, and a cursor reader polling
throughout never sees a gap.

Skipped unless ``CREMIND_TEST_POSTGRES_URL`` names a THROWAWAY database (every
test WIPES its ``public`` schema) and ``psycopg``/``asyncpg`` are importable —
see ``test_search_tool_ids_migration_pg.py`` for a disposable local cluster.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("a2a")

_URL = os.environ.get("CREMIND_TEST_POSTGRES_URL", "").strip()
if not _URL:
    pytest.skip("CREMIND_TEST_POSTGRES_URL is not set (needs a throwaway database)",
                allow_module_level=True)
pytest.importorskip("psycopg")
pytest.importorskip("asyncpg")

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.databases.postgres import PostgresDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260929_profile_working_dir"
_REVISION = "20260930_tags"


@pytest.fixture
def db(monkeypatch):
    url = make_url(_URL)
    provider = PostgresDatabaseProvider(
        host=url.host or "127.0.0.1", port=url.port or 5432,
        database=url.database or "postgres", user=url.username or "postgres",
        password=url.password or "", sslmode="disable",
    )
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    import app.databases as dbs
    import app.storage.migrations as mig

    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    yield provider, mig
    eng.dispose()


def test_upgrade_twice_on_postgres(db) -> None:
    provider, mig = db
    mig.upgrade(_PRIOR_HEAD)
    with provider.sync_engine().begin() as c:
        for name in ("tag_settings", "tag_previews", "tag_commands", "tag_counters", "tag_deliveries",
                     "tag_events", "tag_streams", "tag_devices", "tag_credentials", "tag_companions"):
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
                       "VALUES ('a','alice',0,0,0)"))
    mig.upgrade(_REVISION)
    mig.upgrade(_REVISION)
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert {"tag_deliveries", "tag_streams", "tag_counters"} <= set(insp.get_table_names())
        assert c.execute(text("SELECT COUNT(*) FROM tag_counters")).scalar() == 1
        # No sequence behind the delivery id.
        seqs = c.execute(text("SELECT COUNT(*) FROM pg_class WHERE relkind='S' "
                              "AND relname LIKE 'tag_%'")).scalar()
        assert seqs == 0
        fks = {fk["constrained_columns"][0]: fk["options"].get("ondelete")
               for fk in insp.get_foreign_keys("tag_devices")}
        assert fks == {"companion_id": "CASCADE", "owner_profile": "SET NULL",
                       "bridge_device_id": "SET NULL"}


def test_commit_order_is_seq_order_on_postgres(db) -> None:
    from tests.tags.test_tags_journal import interleaved_appends

    provider, mig = db
    mig.upgrade("head")
    with provider.sync_engine().begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
                       "VALUES ('a','p1',0,0,0)"))
    commits, observed = asyncio.run(interleaved_appends(provider, "p1"))
    seqs = [s for _, s in commits]
    assert seqs == sorted(seqs)
    for snapshot in observed:
        assert snapshot == list(range(1, len(snapshot) + 1))
