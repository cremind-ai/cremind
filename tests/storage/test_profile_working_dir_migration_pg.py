"""Migration 20260929_profile_working_dir on PostgreSQL.

The same story as ``test_profile_working_dir_migration.py``: the server-wide
``server_config.user_working_dir`` becomes the admin's ``profiles.working_dir``
and the key is removed; the downgrade hands it back.

Skipped unless ``CREMIND_TEST_POSTGRES_URL`` names a THROWAWAY database (every
test WIPES its ``public`` schema) and ``psycopg`` is importable — see
``test_search_tool_ids_migration_pg.py`` for a disposable local cluster.
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

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.databases.postgres import PostgresDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260928c_search_tools"


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


def _older_install(db, working_dir: str | None):
    provider, mig = db
    mig.upgrade(_PRIOR_HEAD)
    eng = provider.sync_engine()
    with eng.begin() as c:
        # The baseline builds from live ORM metadata, which already has the
        # column: take it away to look like the older install.
        if "working_dir" in {x["name"] for x in inspect(c).get_columns("profiles")}:
            c.execute(text("ALTER TABLE profiles DROP COLUMN working_dir"))
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
            "VALUES ('pid1','admin',1,1,0), ('pid2','javis',2,2,0)"
        ))
        if working_dir is not None:
            c.execute(text(
                "INSERT INTO server_config (key, value, is_secret, updated_at) "
                "VALUES ('user_working_dir', :v, false, 0)"
            ), {"v": working_dir})
    return eng, mig


def test_the_admin_keeps_the_shared_folder_on_postgres(db) -> None:
    eng, mig = _older_install(db, "/home/me/Documents")
    mig.upgrade("head")
    with eng.connect() as c:
        rows = dict(c.execute(text("SELECT name, working_dir FROM profiles")).all())
        keys = {r[0] for r in c.execute(text("SELECT key FROM server_config")).all()}
    assert rows == {"admin": "/home/me/Documents", "javis": None}
    assert "user_working_dir" not in keys


def test_the_downgrade_hands_the_folder_back_on_postgres(db) -> None:
    eng, mig = _older_install(db, "/srv/work")
    mig.upgrade("head")
    mig.downgrade(_PRIOR_HEAD)
    with eng.connect() as c:
        assert "working_dir" not in {x["name"] for x in inspect(c).get_columns("profiles")}
        value = c.execute(text("SELECT value FROM server_config WHERE key = 'user_working_dir'")).scalar()
    assert value == "/srv/work"
