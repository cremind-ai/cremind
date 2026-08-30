"""Migration 20260827_group_chats upgrades a real pre-feature install.

Three new tables, purely additive — so the interesting part is not that the
tables appear but that the UNIQUE constraint and the ``created_by`` FK survive
the trip. Both carry behaviour rather than tidiness: the unique is the only
thing stopping a boot sweep from double-posting an agent turn, and
``created_by`` SET NULL is what keeps a room alive when the profile that made it
is deleted.

PostgreSQL takes the same DDL but is not exercised here; per CLAUDE.md that
branch is verified manually against a real PG instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260814_task_inbox"
_NEW_HEAD = "20260827_group_chats"

_NEW_TABLES = (
    "group_chats",
    "group_chat_members",
    "group_chat_messages",
)

# Without these every timeline read is a full scan of every group's history.
_EXPECTED_INDEXES = {
    "group_chat_members": {"ix_group_chat_members_profile"},
    "group_chat_messages": {
        "ix_group_chat_messages_group_ordering",
        "ix_group_chat_messages_source_message_id",
    },
}


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    """A database stamped at the head this migration is written against."""
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT)"
        ))
        c.execute(text(
            "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, "
            "profile VARCHAR(128) NOT NULL, kind VARCHAR(16) NOT NULL DEFAULT 'chat', "
            "context_id VARCHAR(256), title VARCHAR(256), created_at FLOAT NOT NULL, "
            "updated_at FLOAT NOT NULL, "
            "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)"
        ))
        c.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        ))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})

        c.execute(text("INSERT INTO profiles VALUES ('pid','dog',0,0)"))
        c.execute(text(
            "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
            "VALUES ('conv1','dog','chat','Chat',0,0)"
        ))


def _upgraded(tmp_path: Path, monkeypatch) -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    # Stopped AT this revision rather than run to "head": what is under test is
    # one migration, and every later one that lands would otherwise move the
    # version this file asserts on.
    mig.upgrade(_NEW_HEAD)
    mig.upgrade(_NEW_HEAD)  # idempotent re-run: the guards must swallow this
    return provider


def test_group_chat_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        tables = set(insp.get_table_names())
        assert set(_NEW_TABLES) <= tables

        for table, expected in _EXPECTED_INDEXES.items():
            assert expected <= {i["name"] for i in insp.get_indexes(table)}

        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            _NEW_HEAD
        )


def test_the_unique_constraint_that_stops_duplicate_posts(
    tmp_path: Path, monkeypatch,
) -> None:
    """It is load-bearing, so it is asserted by name and by effect."""
    provider = _upgraded(tmp_path, monkeypatch)

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        message_uniques = {
            u["name"]: u["column_names"]
            for u in insp.get_unique_constraints("group_chat_messages")
        }
        assert message_uniques["uq_group_chat_messages_source"] == [
            "source_message_id", "segment",
        ]

    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO group_chats (id,name,settings,created_by,created_at,updated_at) "
            "VALUES ('g1','Ops',NULL,'dog',0,0)"
        ))
        c.execute(text(
            "INSERT INTO group_chat_messages "
            "(id,group_id,ordering,sender_kind,sender_name,content,hop,segment,"
            "source_message_id,created_at) "
            "VALUES ('m1','g1',0,'agent','Rex','hi',0,0,'msg-1',0)"
        ))
    with eng.begin() as c:
        with pytest.raises(Exception):
            c.execute(text(
                "INSERT INTO group_chat_messages "
                "(id,group_id,ordering,sender_kind,sender_name,content,hop,segment,"
                "source_message_id,created_at) "
                "VALUES ('m2','g1',1,'agent','Rex','hi',0,0,'msg-1',0)"
            ))

    # A different SEGMENT of the same turn is a different post — an interrupted
    # turn speaks twice and both bubbles have to land.
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO group_chat_messages "
            "(id,group_id,ordering,sender_kind,sender_name,content,hop,segment,"
            "source_message_id,created_at) "
            "VALUES ('m3','g1',1,'agent','Rex','and again',0,1,'msg-1',0)"
        ))

    # NULLs compare distinct, so posts with no source turn are unconstrained —
    # otherwise the second web post in a room would be refused.
    with eng.begin() as c:
        for mid, ordering in (("m4", 2), ("m5", 3)):
            c.execute(text(
                "INSERT INTO group_chat_messages "
                "(id,group_id,ordering,sender_kind,sender_name,content,hop,segment,"
                "created_at) "
                f"VALUES ('{mid}','g1',{ordering},'user','Operator','hi',0,0,0)"
            ))


def test_the_foreign_keys_keep_a_room_alive_but_not_its_members(
    tmp_path: Path, monkeypatch,
) -> None:
    """``created_by`` SET NULL, membership CASCADE — deleting a profile must not
    delete the room the other members are still sitting in."""
    provider = _upgraded(tmp_path, monkeypatch)

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)

        created_by = next(
            f for f in insp.get_foreign_keys("group_chats")
            if f["constrained_columns"] == ["created_by"]
        )
        assert (created_by.get("options") or {}).get("ondelete") == "SET NULL"

        member_fks = {
            tuple(f["constrained_columns"]): (f.get("options") or {}).get("ondelete")
            for f in insp.get_foreign_keys("group_chat_members")
        }
        assert member_fks[("profile",)] == "CASCADE"
        assert member_fks[("group_id",)] == "CASCADE"

        group_fk = next(
            f for f in insp.get_foreign_keys("group_chat_messages")
            if f["constrained_columns"] == ["group_id"]
        )
        assert (group_fk.get("options") or {}).get("ondelete") == "CASCADE"


def test_downgrade_removes_every_new_table(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade("head")
    mig.downgrade(_PRIOR_HEAD)

    eng = provider.sync_engine()
    with eng.connect() as c:
        tables = set(inspect(c).get_table_names())
        assert not (set(_NEW_TABLES) & tables)
        # The tables it did not create are untouched.
        assert {"profiles", "conversations"} <= tables
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            _PRIOR_HEAD
        )


def test_the_revision_id_fits_the_version_column() -> None:
    """``alembic_version.version_num`` is VARCHAR(32).

    Postgres rejects a longer id outright; SQLite silently truncates it, which is
    worse — the head stops matching and every later upgrade re-runs.
    """
    assert len(_NEW_HEAD) <= 32
