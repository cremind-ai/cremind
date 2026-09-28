"""Migration 20260930_tags on a real older install (SQLite).

Built by hand at the prior head (``20260929_profile_working_dir``) with only
the tables the new ones reference, then upgraded twice (the guards make the
re-run a no-op). Pinned: the ten tables and their columns' nullability, the
named indexes and unique constraints, every foreign key's target and
``ondelete``, the seeded ``delivery_id`` counter (exactly one row after two
runs), that ``tag_deliveries.id`` is a plain BIGINT, and that the cascades
really fire (profile -> credentials/streams/events/deliveries/settings, owner
SET NULL; companion -> credentials/devices/commands; device -> deliveries,
previews). The downgrade drops exactly the new tables.

PostgreSQL runs the same DDL in ``test_tags_migration_pg.py`` when a
throwaway database is configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260929_profile_working_dir"
_REVISION = "20260930_tags"
TABLES = {
    "tag_companions", "tag_credentials", "tag_devices", "tag_streams", "tag_events",
    "tag_deliveries", "tag_counters", "tag_commands", "tag_previews", "tag_settings",
}
INDEXES = {
    "tag_credentials": {"ix_tag_credentials_companion", "ix_tag_credentials_profile"},
    "tag_devices": {"ix_tag_devices_owner"},
    "tag_deliveries": {"ix_tag_deliveries_device_created", "ix_tag_deliveries_stage"},
    "tag_commands": {"ix_tag_commands_companion_status"},
}
FKS = {
    ("tag_credentials", "companion_id"): ("tag_companions", "CASCADE"),
    ("tag_credentials", "profile"): ("profiles", "CASCADE"),
    ("tag_devices", "companion_id"): ("tag_companions", "CASCADE"),
    ("tag_devices", "owner_profile"): ("profiles", "SET NULL"),
    ("tag_devices", "bridge_device_id"): ("tag_devices", "SET NULL"),
    ("tag_streams", "profile"): ("profiles", "CASCADE"),
    ("tag_events", "profile"): ("profiles", "CASCADE"),
    ("tag_deliveries", "profile"): ("profiles", "CASCADE"),
    ("tag_deliveries", "companion_id"): ("tag_companions", "SET NULL"),
    ("tag_deliveries", "tag_device_id"): ("tag_devices", "CASCADE"),
    ("tag_deliveries", "event_id"): ("tag_events", "SET NULL"),
    ("tag_commands", "companion_id"): ("tag_companions", "CASCADE"),
    ("tag_previews", "tag_device_id"): ("tag_devices", "CASCADE"),
    ("tag_settings", "profile"): ("profiles", "CASCADE"),
}


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    with provider.sync_engine().begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(36), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT, token_serial INTEGER NOT NULL DEFAULT 0, "
            "working_dir TEXT)"
        ))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('a','alice',0,0)"))
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('b','bob',0,0)"))


@pytest.fixture
def upgraded(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    import app.databases as dbs
    import app.storage.migrations as mig

    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    _build_old_db(provider)
    mig.upgrade(_REVISION)
    mig.upgrade(_REVISION)  # idempotent re-run
    yield provider, mig
    provider.sync_engine().dispose()


def test_upgrade_creates_every_table_index_and_fk(upgraded) -> None:
    provider, _ = upgraded
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert TABLES <= set(insp.get_table_names())
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _REVISION
        for table, names in INDEXES.items():
            assert names <= {i["name"] for i in insp.get_indexes(table)}, table
        found = {}
        for table in TABLES:
            for fk in insp.get_foreign_keys(table):
                found[(table, fk["constrained_columns"][0])] = (
                    fk["referred_table"], fk["options"].get("ondelete"),
                )
        assert found == FKS
        uniques = {
            t: {tuple(u["column_names"]) for u in insp.get_unique_constraints(t)}
            for t in ("tag_devices", "tag_events", "tag_deliveries", "tag_previews")
        }
        assert ("companion_id", "kind", "hw_id") in uniques["tag_devices"]
        assert ("profile", "seq") in uniques["tag_events"]
        assert ("profile", "seq") in uniques["tag_deliveries"]
        assert ("tag_device_id", "kind") in uniques["tag_previews"]
        cols = {col["name"]: col for col in insp.get_columns("tag_deliveries")}
        assert "BIGINT" in str(cols["id"]["type"]).upper()
        assert not cols["stage"]["nullable"] and not cols["expires_at"]["nullable"]
        dev = {col["name"]: col for col in insp.get_columns("tag_devices")}
        assert dev["owner_profile"]["nullable"] and not dev["epoch"]["nullable"]
        # Seeded exactly once despite two runs.
        assert c.execute(text("SELECT COUNT(*), MAX(value) FROM tag_counters "
                              "WHERE name='delivery_id'")).one() == (1, 0)


def test_defaults_apply_to_a_hand_inserted_row(upgraded) -> None:
    provider, _ = upgraded
    with provider.sync_engine().begin() as c:
        c.execute(text("INSERT INTO tag_companions (id, name, created_at, updated_at) VALUES ('c1','pc',0,0)"))
        c.execute(text("INSERT INTO tag_devices (id, companion_id, kind, hw_id, created_at, updated_at) "
                       "VALUES ('d1','c1','tag','T1',0,0)"))
        row = c.execute(text("SELECT epoch, status, clear_required, desired_revision, name "
                             "FROM tag_devices WHERE id='d1'")).one()
    assert tuple(row) == (0, "unclaimed", 0, 0, "")


def test_cascades_fire(upgraded) -> None:
    provider, _ = upgraded
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tag_companions (id, name, created_at, updated_at) VALUES ('c1','pc',0,0)"))
        c.execute(text("INSERT INTO tag_credentials (id, companion_id, kind, profile, secret_sha256, created_at) "
                       "VALUES ('k1','c1','content','alice','x',0), ('k2','c1','hardware',NULL,'x',0)"))
        c.execute(text("INSERT INTO tag_devices (id, companion_id, kind, hw_id, owner_profile, created_at, updated_at) "
                       "VALUES ('d1','c1','tag','T1','alice',0,0), ('b1','c1','bridge','B1',NULL,0,0)"))
        c.execute(text("UPDATE tag_devices SET bridge_device_id='b1' WHERE id='d1'"))
        c.execute(text("INSERT INTO tag_streams (profile, stream_id, updated_at) VALUES ('alice','s',0)"))
        c.execute(text("INSERT INTO tag_events (id, profile, seq, kind, durability, source_type, created_at, expires_at) "
                       "VALUES ('e1','alice',1,'notification','durable','x',0,0)"))
        c.execute(text("INSERT INTO tag_deliveries (id, profile, seq, companion_id, tag_device_id, event_id, kind, "
                       "created_at, updated_at, expires_at) VALUES (1,'alice',1,'c1','d1','e1','notification',0,0,0)"))
        c.execute(text("INSERT INTO tag_previews (id, tag_device_id, kind, png_base64, created_at) "
                       "VALUES ('p1','d1','displayed','x',0)"))
        c.execute(text("INSERT INTO tag_commands (id, companion_id, kind, created_at, expires_at) "
                       "VALUES ('m1','c1','identify',0,0)"))
        c.execute(text("INSERT INTO tag_settings (profile, enabled, updated_at) VALUES ('alice',1,0)"))

    def count(table: str) -> int:
        with eng.connect() as c:
            return int(c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar())

    with eng.begin() as c:
        c.execute(text("DELETE FROM tag_devices WHERE id='b1'"))
    with eng.connect() as c:
        assert c.execute(text("SELECT bridge_device_id FROM tag_devices WHERE id='d1'")).scalar() is None

    with eng.begin() as c:
        c.execute(text("DELETE FROM tag_events WHERE id='e1'"))
    with eng.connect() as c:
        assert c.execute(text("SELECT event_id FROM tag_deliveries WHERE id=1")).scalar() is None

    with eng.begin() as c:
        c.execute(text("DELETE FROM profiles WHERE name='alice'"))
    assert count("tag_streams") == count("tag_events") == count("tag_deliveries") == count("tag_settings") == 0
    assert count("tag_credentials") == 1  # the hardware one stays
    with eng.connect() as c:
        assert c.execute(text("SELECT owner_profile FROM tag_devices WHERE id='d1'")).scalar() is None

    with eng.begin() as c:
        c.execute(text("DELETE FROM tag_devices WHERE id='d1'"))
    assert count("tag_previews") == 0

    with eng.begin() as c:
        c.execute(text("DELETE FROM tag_companions WHERE id='c1'"))
    assert count("tag_credentials") == count("tag_devices") == count("tag_commands") == 0


def test_tags_revision_is_in_the_chain_and_downgrade_removes_only_its_tables(upgraded) -> None:
    provider, mig = upgraded
    # 20261001_tag_setup and 20261002_tag_hosts build on this revision
    # (tests/tags/test_tags_setup_migration.py, tests/tags/test_tag_hosts_migration.py).
    assert list(mig.heads()) == ["20261002_tag_hosts"]
    mig.downgrade(_PRIOR_HEAD)
    with provider.sync_engine().connect() as c:
        names = set(inspect(c).get_table_names())
        assert not (TABLES & names)
        assert "profiles" in names
        assert c.execute(text("SELECT COUNT(*) FROM profiles")).scalar() == 2
    mig.upgrade(_REVISION)
    with provider.sync_engine().connect() as c:
        assert TABLES <= set(inspect(c).get_table_names())


def test_orm_models_match_the_migration(upgraded) -> None:
    """The models carry the same columns as the tables the migration built."""
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401

    provider, _ = upgraded
    # Columns the later revisions (20261001_tag_setup, 20261002_tag_hosts) add to a table of this one.
    later = {"tag_companions": {"mode", "owner_profile", "owner_profile_id", "installation_id", "controller_pub",
                                "generation", "state", "paused", "lease_expires_at", "lease_credential_id",
                                "gateway_device_id", "execution_kind", "host_id"}}
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        for table in TABLES:
            db_cols = {col["name"] for col in insp.get_columns(table)}
            orm_cols = {col.name for col in Base.metadata.tables[table].columns} - later.get(table, set())
            assert db_cols == orm_cols, table
            db_ix = {i["name"] for i in insp.get_indexes(table)}
            orm_ix = {i.name for i in Base.metadata.tables[table].indexes}
            assert orm_ix <= db_ix, table
