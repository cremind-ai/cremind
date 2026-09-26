"""Migration 20260929_profile_working_dir on a real older install (SQLite).

The server-wide ``server_config.user_working_dir`` becomes the admin's own
``profiles.working_dir``; every other profile keeps NULL (its own default).
``profiles`` is the cascade parent of most tables, so the seeded conversation
proves the column was added in place, not by a batch rebuild.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260928c_search_tools"

_OLD_SCHEMA = (
    "CREATE TABLE profiles (id VARCHAR(36) PRIMARY KEY, name VARCHAR(128) NOT NULL, "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, token_serial INTEGER NOT NULL DEFAULT 0)",
    "CREATE UNIQUE INDEX ix_profiles_name ON profiles (name)",
    "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
    "kind VARCHAR(16) NOT NULL DEFAULT 'chat', title VARCHAR(256), "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)",
    "CREATE TABLE server_config (key VARCHAR(256) PRIMARY KEY, value TEXT, "
    "is_secret BOOLEAN NOT NULL DEFAULT 0, updated_at FLOAT)",
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
)


def _build_old_db(provider: SqliteDatabaseProvider, *, working_dir: str | None, admin: bool = True) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        for stmt in _OLD_SCHEMA:
            c.execute(text(stmt))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        if admin:
            c.execute(text("INSERT INTO profiles VALUES ('pid1','admin',1,1,0)"))
            c.execute(text(
                "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
                "VALUES ('conv1','admin','chat','Chat',0,0)"
            ))
        c.execute(text("INSERT INTO profiles VALUES ('pid2','javis',2,2,0)"))
        c.execute(text("INSERT INTO server_config VALUES ('setup_complete','true',0,0)"))
        if working_dir is not None:
            c.execute(text("INSERT INTO server_config VALUES ('user_working_dir',:v,0,0)"), {"v": working_dir})


def _patch(monkeypatch, provider):
    import app.databases as dbs
    import app.storage.migrations as mig

    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    return mig


def _rows(provider) -> tuple[dict, dict]:
    with provider.sync_engine().connect() as c:
        profiles = dict(c.execute(text("SELECT name, working_dir FROM profiles")).all())
        config = dict(c.execute(text("SELECT key, value FROM server_config")).all())
    return profiles, config


def test_the_admin_keeps_the_shared_folder_and_the_others_get_their_own(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)
    _build_old_db(provider, working_dir="~/Documents")

    mig.upgrade("head")
    mig.upgrade("head")  # idempotent

    profiles, config = _rows(provider)
    assert profiles == {"admin": "~/Documents", "javis": None}
    assert "user_working_dir" not in config, "a stale server-wide copy must not survive"
    assert config["setup_complete"] == "true"
    with provider.sync_engine().connect() as c:
        cols = {x["name"]: x for x in inspect(c).get_columns("profiles")}
        assert cols["working_dir"]["nullable"] is True
        # Added in place: the cascade child survives.
        assert c.execute(text("SELECT COUNT(*) FROM conversations")).scalar() == 1
        assert "ix_profiles_name" in {i["name"] for i in inspect(c).get_indexes("profiles")}


def test_no_server_wide_value_leaves_everyone_on_the_default(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)
    _build_old_db(provider, working_dir=None)

    mig.upgrade("head")

    profiles, config = _rows(provider)
    assert profiles == {"admin": None, "javis": None}
    assert "user_working_dir" not in config


def test_without_an_admin_row_the_key_waits_for_the_wizard(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)
    _build_old_db(provider, working_dir="/srv/work", admin=False)

    mig.upgrade("head")

    profiles, config = _rows(provider)
    assert profiles == {"javis": None}
    assert config["user_working_dir"] == "/srv/work"


def test_downgrade_hands_the_admins_folder_back(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)
    _build_old_db(provider, working_dir="D:/Work")

    mig.upgrade("head")
    mig.downgrade(_PRIOR_HEAD)

    with provider.sync_engine().connect() as c:
        assert "working_dir" not in {x["name"] for x in inspect(c).get_columns("profiles")}
        config = dict(c.execute(text("SELECT key, value FROM server_config")).all())
        assert config["user_working_dir"] == "D:/Work"
        assert c.execute(text("SELECT COUNT(*) FROM conversations")).scalar() == 1


def test_downgrade_spells_out_the_admins_default_folder(tmp_path: Path, monkeypatch) -> None:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path / "sys"))
    monkeypatch.delenv("CREMIND_WORKSPACES_DIR", raising=False)
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)
    _build_old_db(provider, working_dir=None)

    mig.upgrade("head")
    mig.downgrade(_PRIOR_HEAD)

    with provider.sync_engine().connect() as c:
        config = dict(c.execute(text("SELECT key, value FROM server_config")).all())
    assert Path(config["user_working_dir"]) == tmp_path / "sys" / "workspaces" / "admin"
