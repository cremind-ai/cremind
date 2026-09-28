"""Migration 20261001_tag_setup on a POPULATED PostgreSQL database at 20260930_tags.

The same checks as ``test_tags_setup_migration.py``: legacy companion rows and
their children survive as ``legacy_shared``, the new schema appears, the
re-run and the downgrade are clean. Skipped unless ``CREMIND_TEST_POSTGRES_URL``
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
from tests.tags.test_tags_setup_migration import (  # noqa: E402
    NEW_COMPANION_COLUMNS, NEW_TABLES, _counts, populate,
)

_PRIOR = "20260930_tags"


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
    assert _counts(eng) == (1, 1, 1, 1)
    with eng.connect() as c:
        insp = inspect(c)
        assert NEW_TABLES <= set(insp.get_table_names())
        assert NEW_COMPANION_COLUMNS <= {col["name"] for col in insp.get_columns("tag_companions")}
        assert c.execute(text("SELECT mode, state, generation, paused FROM tag_companions")).one() == \
            ("legacy_shared", "active", 0, False)
    mig.downgrade(_PRIOR)
    assert _counts(eng) == (1, 1, 1, 1)
    with eng.connect() as c:
        assert not (NEW_TABLES & set(inspect(c).get_table_names()))
    mig.upgrade("head")
    assert _counts(eng) == (1, 1, 1, 1)
