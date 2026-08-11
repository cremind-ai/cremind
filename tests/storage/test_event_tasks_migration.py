"""Migration 20260810_event_tasks upgrades a real pre-feature install.

Rebuilds the four affected tables as they looked at the prior head, stamps that
revision, and runs ``upgrade head``. What matters is that an existing install
comes out valid without a backfill: every standing subscription must read back
as "not a task", and every historical run as "owes nobody a result" — otherwise
the boot sweep would try to deliver years of old runs into people's chats.

PostgreSQL takes the same additive path but is not exercised here; per CLAUDE.md
that branch is verified manually against a real PG instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260720_event_paused"

_OLD_SCHEMA = (
    "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
    "created_at FLOAT, updated_at FLOAT)",
    "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
    "kind VARCHAR(16) NOT NULL DEFAULT 'chat', title VARCHAR(256), "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)",
    "CREATE TABLE skill_event_subscriptions (id VARCHAR(36) PRIMARY KEY, "
    "conversation_id VARCHAR(128) NOT NULL, profile VARCHAR(128) NOT NULL, "
    "skill_name VARCHAR(256) NOT NULL, event_type VARCHAR(128) NOT NULL, "
    "action TEXT NOT NULL, created_at FLOAT NOT NULL, "
    "paused BOOLEAN NOT NULL DEFAULT 0, "
    "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)",
    "CREATE TABLE file_watcher_subscriptions (id VARCHAR(36) PRIMARY KEY, "
    "conversation_id VARCHAR(128) NOT NULL, profile VARCHAR(128) NOT NULL, "
    "name VARCHAR(128) NOT NULL, root_path TEXT NOT NULL, "
    "recursive BOOLEAN NOT NULL DEFAULT 1, target_kind VARCHAR(16) NOT NULL DEFAULT 'any', "
    "event_types VARCHAR(128) NOT NULL, extensions VARCHAR(256), action TEXT NOT NULL, "
    "created_at FLOAT NOT NULL, paused BOOLEAN NOT NULL DEFAULT 0, "
    "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)",
    "CREATE TABLE schedule_event_subscriptions (id VARCHAR(36) PRIMARY KEY, "
    "conversation_id VARCHAR(128) NOT NULL, profile VARCHAR(128) NOT NULL, "
    "title VARCHAR(512) NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '', "
    "all_day BOOLEAN NOT NULL DEFAULT 0, schedule_kind VARCHAR(32) NOT NULL DEFAULT 'instant', "
    "dtstart VARCHAR(32) NOT NULL, duration_minutes INTEGER NOT NULL DEFAULT 30, "
    "rrule TEXT, recurrence_end_type VARCHAR(16), recurrence_end_value VARCHAR(64), "
    "timezone VARCHAR(64), next_fire_at FLOAT, occurrences_fired INTEGER NOT NULL DEFAULT 0, "
    "status VARCHAR(16) NOT NULL DEFAULT 'active', source VARCHAR(16) NOT NULL DEFAULT 'agent', "
    "external_provider VARCHAR(32), external_event_id VARCHAR(256), "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE)",
    "CREATE TABLE event_runs (id VARCHAR(36) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
    "source_kind VARCHAR(16) NOT NULL, subscription_id VARCHAR(36) NOT NULL, "
    "conversation_id VARCHAR(128), run_id VARCHAR(200), "
    "status VARCHAR(16) NOT NULL DEFAULT 'running', label VARCHAR(512) NOT NULL DEFAULT '', "
    "action TEXT NOT NULL DEFAULT '', trigger_payload JSON, pending_question TEXT, error TEXT, "
    "turn_count INTEGER NOT NULL DEFAULT 0, created_at FLOAT NOT NULL, "
    "updated_at FLOAT NOT NULL, finished_at FLOAT, "
    "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE, "
    "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL)",
    "CREATE INDEX ix_event_runs_profile ON event_runs (profile)",
    "CREATE INDEX ix_event_runs_conversation_id ON event_runs (conversation_id)",
    "CREATE INDEX ix_event_runs_status ON event_runs (status)",
    "CREATE INDEX ix_event_runs_sub ON event_runs (source_kind, subscription_id, created_at)",
    "CREATE INDEX ix_event_runs_profile_created ON event_runs (profile, created_at)",
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
)

_SEED = (
    "INSERT INTO profiles VALUES ('pid','p1',0,0)",
    "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
    "VALUES ('conv1','p1','chat','Chat',0,0)",
    "INSERT INTO skill_event_subscriptions "
    "(id,conversation_id,profile,skill_name,event_type,action,created_at,paused) "
    "VALUES ('se1','conv1','p1','gmail','new_email','notify me',0,0)",
    "INSERT INTO file_watcher_subscriptions "
    "(id,conversation_id,profile,name,root_path,recursive,target_kind,event_types,"
    "extensions,action,created_at,paused) "
    "VALUES ('fw1','conv1','p1','w','/tmp',1,'any','created','','log it',0,0)",
    "INSERT INTO schedule_event_subscriptions "
    "(id,conversation_id,profile,title,action,dtstart,created_at,updated_at) "
    "VALUES ('sc1','conv1','p1','Daily','run it','2026-07-01T08:00:00',0,0)",
    "INSERT INTO event_runs "
    "(id,profile,source_kind,subscription_id,conversation_id,status,created_at,updated_at) "
    "VALUES ('er1','p1','skill_event','se1','conv1','completed',0,0)",
)


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        for stmt in _OLD_SCHEMA:
            c.execute(text(stmt))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        for stmt in _SEED:
            c.execute(text(stmt))


def test_event_tasks_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade("head")
    mig.upgrade("head")  # idempotent re-run

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)

        for table in ("skill_event_subscriptions", "file_watcher_subscriptions"):
            cols = {x["name"]: x for x in insp.get_columns(table)}
            assert cols["task"]["nullable"] is False
            for optional in ("task_status", "timeout_at", "completed_at"):
                assert cols[optional]["nullable"] is True, f"{table}.{optional}"

        sched_cols = {x["name"]: x for x in insp.get_columns("schedule_event_subscriptions")}
        assert sched_cols["task"]["nullable"] is False
        assert "task_status" not in sched_cols  # status already carries the lifecycle

        run_cols = {x["name"]: x for x in insp.get_columns("event_runs")}
        assert run_cols["deliver_to_origin"]["nullable"] is False
        assert run_cols["origin_conversation_id"]["nullable"] is True
        assert run_cols["origin_delivered_at"]["nullable"] is True

        # The new FK must SET NULL: a deleted origin conversation degrades a run
        # to "notification only" rather than dangling or cascading it away.
        origin_fk = next(
            f for f in insp.get_foreign_keys("event_runs")
            if f["constrained_columns"] == ["origin_conversation_id"]
        )
        assert (origin_fk.get("options") or {}).get("ondelete", "").upper() == "SET NULL"

        # The batch rebuild must not have dropped the existing indexes.
        idx = {i["name"] for i in insp.get_indexes("event_runs")}
        for name in (
            "ix_event_runs_profile", "ix_event_runs_conversation_id",
            "ix_event_runs_status", "ix_event_runs_sub",
            "ix_event_runs_profile_created", "ix_event_runs_origin_conversation_id",
        ):
            assert name in idx, f"missing index {name}"

        # No backfill: existing rows must read as "not a task" / "owes nothing".
        for table, row_id in (
            ("skill_event_subscriptions", "se1"),
            ("file_watcher_subscriptions", "fw1"),
        ):
            row = c.execute(text(
                f"SELECT task, task_status, timeout_at, completed_at "  # noqa: S608
                f"FROM {table} WHERE id = :i"
            ), {"i": row_id}).fetchone()
            assert row[0] in (0, False)
            assert row[1] is None and row[2] is None and row[3] is None

        assert c.execute(
            text("SELECT task FROM schedule_event_subscriptions WHERE id='sc1'")
        ).scalar() in (0, False)

        run = c.execute(text(
            "SELECT deliver_to_origin, origin_conversation_id, origin_delivered_at "
            "FROM event_runs WHERE id='er1'"
        )).fetchone()
        assert run[0] in (0, False)
        assert run[1] is None and run[2] is None


def test_migration_reaches_the_current_head(tmp_path: Path, monkeypatch) -> None:
    """A missed ``down_revision`` chain would leave installs stuck at the old head.

    The expected head is read from the migration tree rather than hard-coded:
    the property under test is that an install at ``20260720_event_paused``
    upgrades all the way to the tip, whatever the tip currently is. Pinning a
    literal here would fail every time a later feature adds a migration, which
    is noise, not a signal about this one.
    """
    provider = SqliteDatabaseProvider(str(tmp_path / "head.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade("head")

    expected = mig.heads()
    assert len(expected) == 1, f"expected a single head, got {expected}"
    with provider.sync_engine().connect() as c:
        assert c.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar() == expected[0]
