"""Migration 20261002_tag_hosts on a populated install at 20261001_tag_setup (SQLite).

The upgrade must keep every companion (Connect-driven or shared) with its
credentials, devices and operations, mark each ``legacy_external`` through the
server default, add the host tables with their constraints, be a no-op when
re-run, and the downgrade must drop exactly what it added while every row
survives. PostgreSQL: ``test_tag_hosts_migration_pg.py`` when a throwaway
database is set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR = "20261001_tag_setup"
_REVISION = "20261002_tag_hosts"
NEW_TABLES = {"tag_hosts", "tag_host_access", "tag_host_credentials"}
NEW_COLUMNS = {("tag_companions", "execution_kind"), ("tag_companions", "host_id"), ("tag_operations", "host_id")}
FKS = {
    ("tag_host_access", "host_id"): ("tag_hosts", "CASCADE"),
    ("tag_host_access", "profile"): ("profiles", "CASCADE"),
    ("tag_host_credentials", "host_id"): ("tag_hosts", "CASCADE"),
    ("tag_host_credentials", "profile"): ("profiles", "CASCADE"),
}


def populate(eng) -> None:
    """A shared companion (the manual runtime), a private one set up through Connect, and their children."""
    with eng.begin() as c:
        c.execute(text("INSERT INTO tag_companions (id, name, created_by, created_at, updated_at) "
                       "VALUES ('c1', 'Desk PC', 'admin', 0, 0)"))
        c.execute(text("INSERT INTO tag_companions (id, name, created_by, created_at, updated_at, mode, owner_profile, "
                       "owner_profile_id, generation, state, gateway_device_id) VALUES ('c2', 'Office gateway', "
                       "'alice', 0, 0, 'private', 'alice', 'a', 3, 'active', '00112233445566778899aabbccddeeff')"))
        c.execute(text("INSERT INTO tag_credentials (id, companion_id, kind, secret_sha256, created_at) "
                       "VALUES ('tagc_x', 'c1', 'hardware', 'h', 0)"))
        c.execute(text("INSERT INTO tag_credentials (id, companion_id, kind, secret_sha256, created_at) "
                       "VALUES ('tagc_y', 'c2', 'hardware', 'g', 0)"))
        c.execute(text("INSERT INTO tag_devices (id, companion_id, kind, hw_id, created_at, updated_at) "
                       "VALUES ('d1', 'c1', 'tag', '1A2B3C4D', 0, 0)"))
        c.execute(text("INSERT INTO tag_commands (id, companion_id, kind, created_at, expires_at) "
                       "VALUES ('k1', 'c1', 'identify', 0, 1)"))
        c.execute(text("INSERT INTO tag_operations (id, kind, owner_profile, owner_profile_id, companion_id, state, "
                       "stage, created_at, updated_at, expires_at) VALUES ('op1', 'pair_tag', 'alice', 'a', 'c2', "
                       "'running', 'pairing', 0, 0, 1)"))


@pytest.fixture
def mig_env(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    import app.databases as dbs
    import app.storage.migrations as mig

    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    mig.upgrade(_PRIOR)
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
                       "VALUES ('a', 'alice', 0, 0, 0)"))
    populate(eng)
    yield provider, mig, eng


def counts(eng) -> tuple[int, ...]:
    with eng.connect() as c:
        return tuple(int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar())
                     for t in ("tag_companions", "tag_credentials", "tag_devices", "tag_commands", "tag_operations"))


def columns(insp, table: str) -> set[str]:
    return {col["name"] for col in insp.get_columns(table)}


def test_upgrade_keeps_every_worker_as_legacy_external_and_adds_the_host_schema(mig_env) -> None:
    provider, mig, eng = mig_env
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
    before = counts(eng)
    mig.upgrade("head")
    mig.upgrade("head")  # re-run: a no-op
    assert counts(eng) == before == (2, 2, 1, 1, 1)
    with eng.connect() as c:
        insp = inspect(c)
        assert NEW_TABLES <= set(insp.get_table_names())
        for table, column in NEW_COLUMNS:
            assert column in columns(insp, table), (table, column)
        rows = c.execute(text("SELECT id, mode, execution_kind, host_id, generation FROM tag_companions "
                              "ORDER BY id")).all()
        assert [tuple(r) for r in rows] == [("c1", "legacy_shared", "legacy_external", None, 0),
                                            ("c2", "private", "legacy_external", None, 3)]
        assert c.execute(text("SELECT host_id, state FROM tag_operations")).one() == (None, "running")
        for (table, column), (target, ondelete) in FKS.items():
            fk = next(f for f in insp.get_foreign_keys(table) if f["constrained_columns"] == [column])
            assert fk["referred_table"] == target
            assert (fk.get("options") or {}).get("ondelete", "").upper() == ondelete
        assert "uq_tag_host_access_host_profile" in {u["name"] for u in insp.get_unique_constraints("tag_host_access")}
        assert "ix_tag_operations_host" in {i["name"] for i in insp.get_indexes("tag_operations")}
        assert "ix_tag_host_credentials_host" in {i["name"] for i in insp.get_indexes("tag_host_credentials")}


def test_a_new_companion_defaults_to_legacy_external(mig_env) -> None:
    """A Connect that registers after the upgrade (the old setup API) still gets a legacy worker."""
    provider, mig, eng = mig_env
    mig.upgrade("head")
    with eng.begin() as c:
        c.execute(text("INSERT INTO tag_companions (id, name, created_by, created_at, updated_at) "
                       "VALUES ('c3', 'Later', 'admin', 0, 0)"))
        assert c.execute(text("SELECT execution_kind FROM tag_companions WHERE id='c3'")).scalar() == "legacy_external"


def test_downgrade_drops_only_what_the_revision_added(mig_env) -> None:
    provider, mig, eng = mig_env
    mig.upgrade("head")
    with eng.begin() as c:
        c.execute(text("INSERT INTO tag_hosts (id, kind, created_at, updated_at) VALUES ('h1', 'server', 0, 0)"))
        c.execute(text("UPDATE tag_companions SET execution_kind='server', host_id='h1' WHERE id='c2'"))
    mig.downgrade(_PRIOR)
    with eng.connect() as c:
        insp = inspect(c)
        assert not (NEW_TABLES & set(insp.get_table_names()))
        for table, column in NEW_COLUMNS:
            assert column not in columns(insp, table), (table, column)
    assert counts(eng) == (2, 2, 1, 1, 1)
    mig.upgrade("head")
    assert counts(eng) == (2, 2, 1, 1, 1)


def test_orm_models_match_the_migration(mig_env) -> None:
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401

    provider, mig, eng = mig_env
    mig.upgrade(_REVISION)
    with eng.connect() as c:
        insp = inspect(c)
        for table in NEW_TABLES | {"tag_companions", "tag_operations"}:
            assert columns(insp, table) == {col.name for col in Base.metadata.tables[table].columns}, table
            db_ix = {i["name"] for i in insp.get_indexes(table)}
            orm_ix = {i.name for i in Base.metadata.tables[table].indexes}
            assert orm_ix <= db_ix, table
