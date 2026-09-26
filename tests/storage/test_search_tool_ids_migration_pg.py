"""The search-tool rename migrations on PostgreSQL.

The same scenarios as ``test_search_tool_ids_migration.py`` (see
``_search_rename_scenarios``), against a real PostgreSQL — the backend where
the table rename takes the other path (``ALTER TABLE/INDEX … RENAME``,
``RENAME CONSTRAINT`` for the UNIQUEs and PostgreSQL's own ``*_pkey`` /
``*_fkey`` names) and where the whole upgrade is one transaction.

Skipped unless ``CREMIND_TEST_POSTGRES_URL`` names a THROWAWAY database, e.g.
``postgresql://postgres@127.0.0.1:55432/cremind_test`` — every test WIPES its
``public`` schema — and the ``psycopg`` driver is importable. A local
disposable cluster is enough::

    initdb -D <dir> -U postgres -A trust
    pg_ctl -D <dir> -o "-p 55432" -l <dir>/log start
    createdb -h 127.0.0.1 -p 55432 -U postgres cremind_test
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

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.databases.postgres import PostgresDatabaseProvider  # noqa: E402
from tests.storage import _search_rename_scenarios as S  # noqa: E402


@pytest.fixture
def db(monkeypatch):
    url = make_url(_URL)
    provider = PostgresDatabaseProvider(
        host=url.host or "127.0.0.1",
        port=url.port or 5432,
        database=url.database or "postgres",
        user=url.username or "postgres",
        password=url.password or "",
        sslmode="disable",
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


def _at(db, revision: str, *seeders):
    provider, mig = db
    mig.upgrade(revision)
    eng = provider.sync_engine()
    S.drop_new_conversation_columns(eng)
    with eng.begin() as c:
        for seed in seeders:
            seed(c)
    return eng, mig


def test_v0018_install_moves_the_manual_search_per_profile(db) -> None:
    eng, mig = _at(db, S.V0018_HEAD, S.seed_common)
    mig.upgrade("head")
    assert S.version(eng) == S.HEAD
    with eng.connect() as c:
        S.assert_tools_after_swap(c, with_personal=False)
        assert S.usage_of(c)["u1"] == ("cremind_documentation_search", "tool", 10)
        assert S.usage_of(c)["u2"] == ("cremind_documentation_search", "tool", 20)
        S.assert_conversations_upgraded(c)
    S.assert_document_schema(eng)
    before = S.snapshot(eng)
    mig.upgrade("head")
    assert S.snapshot(eng) == before


def test_dev_install_swaps_in_order_and_renames_everything(db) -> None:
    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev, S.seed_renamed_boot)
    mig.upgrade("head")
    assert S.version(eng) == S.HEAD
    with eng.connect() as c:
        S.assert_tools_after_swap(c, with_personal=True)
        usage = S.usage_of(c)
        assert usage["u5"] == ("documentation_search", "documents", 100)
        assert usage["u7"] == ("documentation_search", "documents", 50)
        assert usage["u1"] == ("cremind_documentation_search", "tool", 10)
        S.assert_dev_data_at_head(c)
        S.assert_conversations_upgraded(c)
    S.assert_document_schema(eng)
    before = S.snapshot(eng)
    mig.upgrade("head")
    assert S.snapshot(eng) == before


def test_fresh_install_and_downgrade_round_trip(db) -> None:
    provider, mig = db
    mig.upgrade("head")
    eng = provider.sync_engine()
    S.assert_document_schema(eng)

    mig.downgrade(S.DEV_HEAD)
    S.assert_document_schema(eng, legacy=True)
    mig.upgrade("head")
    S.assert_document_schema(eng)
