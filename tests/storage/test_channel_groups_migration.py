"""Migration 20260829_channel_groups on a real pre-feature install.

Two new tables and one dropped, so there are three interesting parts:

- the ``UNIQUE(channel_id, platform_chat_id)`` that makes "have we seen this
  chat before?" one indexed lookup AND arbitrates the discovery race — without
  it two messages arriving on the same tick would both create a pending row and
  the operator would be asked twice about one group;
- the ``conversation_id`` FK being SET NULL rather than CASCADE, so deleting a
  transcript cannot silently un-approve the group and start asking again;
- ``group_chat_channel_bindings`` going away. It shipped in no release —
  ``20260827_group_chats`` was edited in place to stop creating it — so the drop
  only ever fires on a development database that ran the earlier version. Both
  starting states are exercised here, because the guard is the only thing
  standing between the second one and a crash on boot.

PostgreSQL takes the same DDL but is not exercised here; per CLAUDE.md that
branch is verified manually against a real PG instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260827_group_chats"
_NEW_HEAD = "20260829_channel_groups"

_NEW_TABLES = ("channel_groups", "channel_group_members")

# Without these, every inbound message full-scans every group of every channel.
_EXPECTED_INDEXES = {
    "channel_groups": {
        "ix_channel_groups_channel_id",
        "ix_channel_groups_profile",
        "ix_channel_groups_conversation_id",
    },
    "channel_group_members": {"ix_channel_group_members_group_id"},
}

_OBSOLETE = "group_chat_channel_bindings"


def _build_old_db(
    provider: SqliteDatabaseProvider, *, with_bindings: bool = False,
) -> None:
    """A database stamped at the head this migration is written against."""
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT)"
        ))
        c.execute(text(
            "CREATE TABLE channels (id VARCHAR(36) PRIMARY KEY, "
            "profile VARCHAR(128) NOT NULL, channel_type VARCHAR(32) NOT NULL, "
            "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
            "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)"
        ))
        c.execute(text(
            "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, "
            "profile VARCHAR(128) NOT NULL, kind VARCHAR(16) NOT NULL DEFAULT 'chat', "
            "context_id VARCHAR(256), title VARCHAR(256), created_at FLOAT NOT NULL, "
            "updated_at FLOAT NOT NULL, "
            "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)"
        ))
        c.execute(text("CREATE TABLE group_chats (id VARCHAR(36) PRIMARY KEY)"))
        if with_bindings:
            # Exactly what the first version of 20260827 created.
            c.execute(text(
                f"CREATE TABLE {_OBSOLETE} (id VARCHAR(36) PRIMARY KEY, "
                "group_id VARCHAR(36) NOT NULL, channel_type VARCHAR(32) NOT NULL, "
                "platform_chat_id VARCHAR(128) NOT NULL, title VARCHAR(256), "
                "created_at FLOAT NOT NULL)"
            ))
            c.execute(text(
                f"CREATE INDEX ix_group_chat_channel_bindings_group_id "
                f"ON {_OBSOLETE} (group_id)"
            ))
            c.execute(text(
                f"INSERT INTO {_OBSOLETE} VALUES ('b1','g1','telegram','-100','Ops',0)"
            ))
        c.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        ))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})

        c.execute(text("INSERT INTO profiles VALUES ('pid','dog',0,0)"))
        c.execute(text(
            "INSERT INTO channels VALUES ('ch1','dog','telegram',0,0)"
        ))
        c.execute(text(
            "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
            "VALUES ('conv1','dog','chat','Ops room',0,0)"
        ))


def _upgraded(
    tmp_path: Path, monkeypatch, *, with_bindings: bool = False,
) -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider, with_bindings=with_bindings)
    # Stopped AT this revision rather than run to "head": what is under test is
    # one migration, and every later one that lands would otherwise move the
    # version this file asserts on.
    mig.upgrade(_NEW_HEAD)
    mig.upgrade(_NEW_HEAD)  # idempotent re-run: the guards must swallow this
    return provider


def test_channel_group_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
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


def test_a_dev_database_that_ran_the_old_migration_loses_the_bindings_table(
    tmp_path: Path, monkeypatch,
) -> None:
    """The one destructive step, and the only one that ever fires in practice."""
    provider = _upgraded(tmp_path, monkeypatch, with_bindings=True)

    with provider.sync_engine().connect() as c:
        assert _OBSOLETE not in set(inspect(c).get_table_names())


def test_a_fresh_database_upgrades_without_the_bindings_table(
    tmp_path: Path, monkeypatch,
) -> None:
    """The guard is what makes the drop safe on an install that never had it."""
    provider = _upgraded(tmp_path, monkeypatch, with_bindings=False)

    with provider.sync_engine().connect() as c:
        tables = set(inspect(c).get_table_names())
        assert _OBSOLETE not in tables
        assert set(_NEW_TABLES) <= tables


def test_one_chat_belongs_to_one_group_per_channel(
    tmp_path: Path, monkeypatch,
) -> None:
    """Asserted by name and by effect: this unique is what settles the discovery
    race, so a rename that broke it would surface as duplicate approvals."""
    provider = _upgraded(tmp_path, monkeypatch)
    eng = provider.sync_engine()

    with eng.connect() as c:
        uniques = {
            u["name"]: u["column_names"]
            for u in inspect(c).get_unique_constraints("channel_groups")
        }
        assert uniques["uq_channel_groups_chat"] == [
            "channel_id", "platform_chat_id",
        ]

    with eng.begin() as c:
        c.execute(text(_INSERT_GROUP), {"id": "g1", "chat": "-100"})
    # A different chat on the same channel is fine…
    with eng.begin() as c:
        c.execute(text(_INSERT_GROUP), {"id": "g2", "chat": "-200"})
    # …the same one twice is not.
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text(_INSERT_GROUP), {"id": "g3", "chat": "-100"})


def test_one_member_row_per_person_per_group(tmp_path: Path, monkeypatch) -> None:
    """What makes the upsert on every inbound message safe to run."""
    provider = _upgraded(tmp_path, monkeypatch)
    eng = provider.sync_engine()

    with eng.connect() as c:
        uniques = {
            u["name"]: u["column_names"]
            for u in inspect(c).get_unique_constraints("channel_group_members")
        }
        assert uniques["uq_channel_group_members"] == ["group_id", "member_id"]

    with eng.begin() as c:
        c.execute(text(_INSERT_GROUP), {"id": "g1", "chat": "-100"})
        c.execute(text(_INSERT_MEMBER), {"id": "m1", "member": "u1"})
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text(_INSERT_MEMBER), {"id": "m2", "member": "u1"})


def test_deleting_the_transcript_keeps_the_approval(
    tmp_path: Path, monkeypatch,
) -> None:
    """SET NULL, not CASCADE. Losing the conversation must not silently
    un-approve the group and start asking about it again."""
    provider = _upgraded(tmp_path, monkeypatch)
    eng = provider.sync_engine()

    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
        c.execute(text(
            "INSERT INTO channel_groups (id,channel_id,profile,platform_chat_id,"
            "status,discovered_via,conversation_id,created_at,updated_at) "
            "VALUES ('g1','ch1','dog','-100','approved','join','conv1',0,0)"
        ))
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
        c.execute(text("DELETE FROM conversations WHERE id='conv1'"))

    with eng.connect() as c:
        row = c.execute(text(
            "SELECT status, conversation_id FROM channel_groups WHERE id='g1'"
        )).one()
        assert row.status == "approved"
        assert row.conversation_id is None


def test_the_revision_id_fits_the_version_column(tmp_path: Path, monkeypatch) -> None:
    """``alembic_version.version_num`` is VARCHAR(32): Postgres rejects a longer
    id and SQLite silently truncates it."""
    assert len(_NEW_HEAD) <= 32


_INSERT_GROUP = (
    "INSERT INTO channel_groups (id,channel_id,profile,platform_chat_id,"
    "status,discovered_via,created_at,updated_at) "
    "VALUES (:id,'ch1','dog',:chat,'pending','message',0,0)"
)

_INSERT_MEMBER = (
    "INSERT INTO channel_group_members (id,group_id,member_id,source,is_bot,"
    "message_count) VALUES (:id,'g1',:member,'seen',0,0)"
)
