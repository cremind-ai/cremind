"""Migration 20261002_tag_hosts on a POPULATED PostgreSQL database at 20261001_tag_setup.

The same checks as ``test_tag_hosts_migration.py``: every companion and its
children survive as ``legacy_external``, the host schema appears, the re-run
and the downgrade are clean. Skipped unless ``CREMIND_TEST_POSTGRES_URL``
names a THROWAWAY database (every test WIPES its ``public`` schema).
"""

from __future__ import annotations

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
from tests.tags.test_tag_hosts_migration import (  # noqa: E402
    FKS, NEW_COLUMNS, NEW_TABLES, columns, counts, populate,
)

_PRIOR = "20261001_tag_setup"


@pytest.fixture
def pg(monkeypatch):
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
    mig.upgrade(_PRIOR)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
                       "VALUES ('a', 'alice', 0, 0, 0)"))
    populate(eng)
    yield provider, mig, eng
    eng.dispose()


def test_upgrade_on_a_populated_postgres(pg) -> None:
    provider, mig, eng = pg
    mig.upgrade("head")
    mig.upgrade("head")
    assert counts(eng) == (2, 2, 1, 1, 1)
    with eng.connect() as c:
        insp = inspect(c)
        assert NEW_TABLES <= set(insp.get_table_names())
        for table, column in NEW_COLUMNS:
            assert column in columns(insp, table), (table, column)
        rows = c.execute(text("SELECT id, execution_kind, host_id FROM tag_companions ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [("c1", "legacy_external", None), ("c2", "legacy_external", None)]
        for (table, column), (target, ondelete) in FKS.items():
            fk = next(f for f in insp.get_foreign_keys(table) if f["constrained_columns"] == [column])
            assert fk["referred_table"] == target
            assert (fk.get("options") or {}).get("ondelete", "").upper() == ondelete
        assert "ix_tag_operations_host" in {i["name"] for i in insp.get_indexes("tag_operations")}
    with eng.begin() as c:
        c.execute(text("INSERT INTO tag_hosts (id, kind, created_at, updated_at) VALUES ('h1', 'server', 0, 0)"))
        c.execute(text("INSERT INTO tag_host_access (id, host_id, profile, profile_id, created_at) "
                       "VALUES ('g1', 'h1', 'alice', 'a', 0)"))
    mig.downgrade(_PRIOR)
    assert counts(eng) == (2, 2, 1, 1, 1)
    with eng.connect() as c:
        insp = inspect(c)
        assert not (NEW_TABLES & set(insp.get_table_names()))
        for table, column in NEW_COLUMNS:
            assert column not in columns(insp, table), (table, column)
    mig.upgrade("head")
    assert counts(eng) == (2, 2, 1, 1, 1)
