"""Migration 20261001_tag_setup on a populated install at 20260930_tags (SQLite).

The upgrade must leave every existing companion (and its credentials, devices,
commands) in place as ``legacy_shared`` — no table rebuild, so no cascade —
add the new tables and indexes, be a no-op when re-run, and the downgrade
must drop exactly what it added while the legacy rows survive.
PostgreSQL: ``test_tags_setup_migration_pg.py`` when a throwaway database is set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR = "20260930_tags"
_REVISION = "20261001_tag_setup"
NEW_TABLES = {
    "tag_authority", "tag_connect_installations", "tag_bindings", "tag_setup_sessions", "tag_operations",
    "tag_vault", "tag_revocations", "tag_idempotency",
}
NEW_COMPANION_COLUMNS = {
    "mode", "owner_profile", "owner_profile_id", "installation_id", "controller_pub", "generation", "state",
    "paused", "lease_expires_at", "lease_credential_id", "gateway_device_id",
}
FKS = {
    ("tag_bindings", "companion_id"): ("tag_companions", "CASCADE"),
    ("tag_bindings", "tag_device_id"): ("tag_devices", "SET NULL"),
    ("tag_setup_sessions", "owner_profile"): ("profiles", "CASCADE"),
    ("tag_operations", "owner_profile"): ("profiles", "CASCADE"),
    ("tag_operations", "companion_id"): ("tag_companions", "SET NULL"),
    ("tag_vault", "companion_id"): ("tag_companions", "CASCADE"),
}


def populate(eng) -> None:
    with eng.begin() as c:
        c.execute(text("INSERT INTO tag_companions (id, name, created_by, created_at, updated_at) "
                       "VALUES ('c1', 'Desk PC', 'admin', 0, 0)"))
        c.execute(text("INSERT INTO tag_credentials (id, companion_id, kind, secret_sha256, created_at) "
                       "VALUES ('tagc_x', 'c1', 'hardware', 'h', 0)"))
        c.execute(text("INSERT INTO tag_devices (id, companion_id, kind, hw_id, created_at, updated_at) "
                       "VALUES ('d1', 'c1', 'tag', '1A2B3C4D', 0, 0)"))
        c.execute(text("INSERT INTO tag_commands (id, companion_id, kind, created_at, expires_at) "
                       "VALUES ('k1', 'c1', 'identify', 0, 1)"))


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


def _counts(eng) -> tuple[int, int, int, int]:
    with eng.connect() as c:
        return tuple(int(c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar())
                     for t in ("tag_companions", "tag_credentials", "tag_devices", "tag_commands"))


def test_upgrade_keeps_legacy_rows_and_adds_the_setup_schema(mig_env) -> None:
    provider, mig, eng = mig_env
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
    before = _counts(eng)
    mig.upgrade("head")
    mig.upgrade("head")  # re-run: a no-op
    assert _counts(eng) == before == (1, 1, 1, 1)
    with eng.connect() as c:
        insp = inspect(c)
        names = set(insp.get_table_names())
        assert NEW_TABLES <= names
        cols = {col["name"] for col in insp.get_columns("tag_companions")}
        assert NEW_COMPANION_COLUMNS <= cols
        assert c.execute(text("SELECT mode, state, generation, paused FROM tag_companions")).one() == \
            ("legacy_shared", "active", 0, 0)
        for (table, column), (target, ondelete) in FKS.items():
            fk = next(f for f in insp.get_foreign_keys(table) if f["constrained_columns"] == [column])
            assert fk["referred_table"] == target
            assert (fk.get("options") or {}).get("ondelete", "").upper() == ondelete
        uniques = {u["name"] for u in insp.get_unique_constraints("tag_bindings")}
        assert "uq_tag_bindings_device" in uniques
        assert {"ix_tag_bindings_companion", "ix_tag_bindings_owner"} <= {i["name"] for i in insp.get_indexes("tag_bindings")}


def test_downgrade_drops_only_what_the_revision_added(mig_env) -> None:
    provider, mig, eng = mig_env
    mig.upgrade("head")
    mig.downgrade(_PRIOR)
    with eng.connect() as c:
        insp = inspect(c)
        assert not (NEW_TABLES & set(insp.get_table_names()))
        assert not (NEW_COMPANION_COLUMNS & {col["name"] for col in insp.get_columns("tag_companions")})
    assert _counts(eng) == (1, 1, 1, 1)
    mig.upgrade("head")
    assert _counts(eng) == (1, 1, 1, 1)


def test_orm_models_match_the_migration(mig_env) -> None:
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401

    provider, mig, eng = mig_env
    mig.upgrade("head")
    with eng.connect() as c:
        insp = inspect(c)
        for table in NEW_TABLES | {"tag_companions"}:
            db_cols = {col["name"] for col in insp.get_columns(table)}
            orm_cols = {col.name for col in Base.metadata.tables[table].columns}
            assert db_cols == orm_cols, table
            db_ix = {i["name"] for i in insp.get_indexes(table)}
            orm_ix = {i.name for i in Base.metadata.tables[table].indexes}
            assert orm_ix <= db_ix, table
