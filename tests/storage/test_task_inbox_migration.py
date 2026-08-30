"""Migration 20260814_task_inbox upgrades a real pre-feature install.

One nullable column, so the interesting part is not the column — it is that the
SQLite batch rebuild does not quietly take the table's indexes with it, and that
existing rows come out reading as "delivered before modes were recorded" rather
than as something the surfaces would mislabel.

PostgreSQL takes the same additive path but is not exercised here; per CLAUDE.md
that branch is verified manually against a real PG instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260813_token_serial"
_NEW_HEAD = "20260814_task_inbox"

# Every index event_runs is expected to carry. SQLite implements ALTER as a
# table copy, so an unguarded batch drops these — the failure is silent and only
# shows up later as a slow Events page.
_EXPECTED_INDEXES = {
    "ix_event_runs_profile",
    "ix_event_runs_conversation_id",
    "ix_event_runs_status",
    "ix_event_runs_origin_conversation_id",
    "ix_event_runs_sub",
    "ix_event_runs_profile_created",
}


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    """The event_runs table exactly as 20260810_event_tasks leaves it."""
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT)"
        ))
        c.execute(text(
            "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, "
            "profile VARCHAR(128) NOT NULL, kind VARCHAR(16) NOT NULL DEFAULT 'chat', "
            "title VARCHAR(256), created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
            "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)"
        ))
        c.execute(text(
            "CREATE TABLE event_runs (id VARCHAR(36) PRIMARY KEY, "
            "profile VARCHAR(128) NOT NULL, source_kind VARCHAR(16) NOT NULL, "
            "subscription_id VARCHAR(36) NOT NULL, conversation_id VARCHAR(128), "
            "run_id VARCHAR(200), status VARCHAR(16) NOT NULL DEFAULT 'running', "
            "label VARCHAR(512) NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '', "
            "trigger_payload JSON, pending_question TEXT, error TEXT, "
            "turn_count INTEGER NOT NULL DEFAULT 0, created_at FLOAT NOT NULL, "
            "updated_at FLOAT NOT NULL, finished_at FLOAT, "
            "origin_conversation_id VARCHAR(128), "
            "deliver_to_origin BOOLEAN NOT NULL DEFAULT 0, origin_delivered_at FLOAT, "
            "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE, "
            "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL, "
            "CONSTRAINT fk_event_runs_origin_conversation_id_conversations "
            "FOREIGN KEY(origin_conversation_id) REFERENCES conversations(id) "
            "ON DELETE SET NULL)"
        ))
        for name, cols in (
            ("ix_event_runs_profile", "profile"),
            ("ix_event_runs_conversation_id", "conversation_id"),
            ("ix_event_runs_status", "status"),
            ("ix_event_runs_origin_conversation_id", "origin_conversation_id"),
            ("ix_event_runs_sub", "source_kind, subscription_id, created_at"),
            ("ix_event_runs_profile_created", "profile, created_at"),
        ):
            c.execute(text(f"CREATE INDEX {name} ON event_runs ({cols})"))
        c.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        ))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})

        c.execute(text("INSERT INTO profiles VALUES ('pid','p1',0,0)"))
        c.execute(text(
            "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
            "VALUES ('conv1','p1','chat','Chat',0,0)"
        ))
        # A task run already delivered by the old code, and an ordinary run.
        c.execute(text(
            "INSERT INTO event_runs (id,profile,source_kind,subscription_id,"
            "conversation_id,status,created_at,updated_at,origin_conversation_id,"
            "deliver_to_origin,origin_delivered_at) "
            "VALUES ('er1','p1','skill_event','se1','conv1','completed',0,0,"
            "'conv1',1,12345.0)"
        ))
        c.execute(text(
            "INSERT INTO event_runs (id,profile,source_kind,subscription_id,"
            "conversation_id,status,created_at,updated_at) "
            "VALUES ('er2','p1','schedule','sc1','conv1','completed',0,0)"
        ))


def test_task_inbox_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    # Targeted at this revision rather than "head": later migrations chain onto
    # it, and this file is about what THIS one does to a pre-feature install.
    mig.upgrade(_NEW_HEAD)
    mig.upgrade(_NEW_HEAD)  # idempotent re-run

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        cols = {x["name"]: x for x in insp.get_columns("event_runs")}
        assert cols["origin_delivery_mode"]["nullable"] is True

        # The batch rebuild must not have eaten the indexes.
        assert _EXPECTED_INDEXES <= {i["name"] for i in insp.get_indexes("event_runs")}

        # The origin FK must still SET NULL, or a deleted conversation would
        # cascade away run history (or dangle).
        origin_fk = next(
            f for f in insp.get_foreign_keys("event_runs")
            if f["constrained_columns"] == ["origin_conversation_id"]
        )
        assert (origin_fk.get("options") or {}).get("ondelete") == "SET NULL"

        # No backfill: an already-delivered row reads as "mode unknown", which
        # the CLI and the Events page render as a plain "delivered".
        rows = dict(c.execute(text(
            "SELECT id, origin_delivery_mode FROM event_runs"
        )).all())
        assert rows == {"er1": None, "er2": None}

        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            _NEW_HEAD
        )


def test_downgrade_removes_the_column_and_keeps_the_indexes(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade(_NEW_HEAD)
    mig.downgrade(_PRIOR_HEAD)

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        assert "origin_delivery_mode" not in {x["name"] for x in insp.get_columns("event_runs")}
        assert _EXPECTED_INDEXES <= {i["name"] for i in insp.get_indexes("event_runs")}


def test_the_revision_id_fits_the_version_column(tmp_path: Path) -> None:
    """``alembic_version.version_num`` is VARCHAR(32).

    Postgres rejects a longer id outright; SQLite silently truncates it, which
    is worse — the head stops matching and every later upgrade re-runs.
    """
    assert len(_NEW_HEAD) <= 32
