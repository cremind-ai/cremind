"""Migration 20260813_token_serial upgrades a real install.

Rebuilds ``profiles`` as it looked at the prior head, stamps that revision, and
runs ``upgrade head``. Two things need proving beyond "the column exists":

1. **Child rows survive.** ``profiles`` is the FK parent of ~12 ``ON DELETE
   CASCADE`` tables, and ``app/databases/sqlite.py`` turns on
   ``PRAGMA foreign_keys``. Alembic's SQLite batch mode implements ALTER as
   ``DROP TABLE`` + recreate, and SQLite's ``DROP TABLE`` fires cascades — so a
   batch-wrapped version of this migration silently deletes every conversation
   in the install while every other assertion still passes. That is why the
   migration uses a plain ``op.add_column``, and why the seeded conversation
   below is the real point of this file.
2. **Existing rows backfill to 0**, which is what keeps pre-feature tokens
   (no ``tsr`` claim, read as 0) working until their profile is first rotated.

PostgreSQL takes the same additive path but is not exercised here; per CLAUDE.md
that branch is verified manually against a real PG instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260812_sender_confirm"

_OLD_SCHEMA = (
    # profiles at the prior head: no token_serial, with the UNIQUE name index.
    "CREATE TABLE profiles (id VARCHAR(36) PRIMARY KEY, name VARCHAR(128) NOT NULL, "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL)",
    "CREATE UNIQUE INDEX ix_profiles_name ON profiles (name)",
    "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
    "kind VARCHAR(16) NOT NULL DEFAULT 'chat', title VARCHAR(256), "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)",
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
)

_SEED = (
    "INSERT INTO profiles VALUES ('pid1','admin',1,1)",
    "INSERT INTO profiles VALUES ('pid2','bob',2,2)",
    "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
    "VALUES ('conv1','admin','chat','Chat',0,0)",
)


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        for stmt in _OLD_SCHEMA:
            c.execute(text(stmt))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        for stmt in _SEED:
            c.execute(text(stmt))


def _patch(monkeypatch, provider):
    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    return mig


def test_token_serial_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)

    _build_old_db(provider)
    mig.upgrade("head")
    mig.upgrade("head")  # idempotent re-run

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        cols = {x["name"]: x for x in insp.get_columns("profiles")}
        assert "token_serial" in cols
        assert cols["token_serial"]["nullable"] is False

        # Existing profiles start at 0, so a token minted before this feature
        # (which carries no ``tsr`` claim, read as 0) keeps working.
        rows = dict(c.execute(text("SELECT name, token_serial FROM profiles")).all())
        assert rows == {"admin": 0, "bob": 0}

        # The name index must survive AND stay UNIQUE — re-asserting it as a
        # plain index (the shape the batch-mode migrations use) would make
        # duplicate profile names insertable.
        name_idx = [i for i in insp.get_indexes("profiles") if i["name"] == "ix_profiles_name"]
        assert name_idx, "ix_profiles_name was dropped by the migration"
        assert name_idx[0]["unique"]  # SQLite reflects this as 1, not True

    # ...and it must still actually reject a duplicate name.
    with eng.begin() as c:
        with pytest.raises(IntegrityError):
            c.execute(text("INSERT INTO profiles VALUES ('pid3','admin',3,3,0)"))

    # THE regression guard: `profiles` is a cascade parent, so any migration
    # that rebuilds the table (op.batch_alter_table on SQLite) wipes the
    # children. If this ever fails, the migration went back to batch mode.
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM conversations")).scalar() == 1


def test_token_serial_downgrade_removes_the_column(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)

    _build_old_db(provider)
    mig.upgrade("head")
    mig.downgrade(_PRIOR_HEAD)

    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert "token_serial" not in {x["name"] for x in insp.get_columns("profiles")}
        assert "ix_profiles_name" in {i["name"] for i in insp.get_indexes("profiles")}
        # The downgrade must not cascade either.
        assert c.execute(text("SELECT COUNT(*) FROM conversations")).scalar() == 1
