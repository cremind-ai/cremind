"""Migration 20260812_sender_send_confirmation upgrades a real install.

Rebuilds ``channel_senders`` as it looked at the prior head, stamps that
revision, and runs ``upgrade head``. The column is additive, so what needs
proving is that existing rows come out reading as "inherit the profile setting"
(NULL) — anything else would silently change who the agent messages without
asking — and that the SQLite batch rebuild preserves
UNIQUE(channel_id, sender_id) and the channel index.

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

_PRIOR_HEAD = "20260811_sender_phone"

_OLD_SCHEMA = (
    "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
    "created_at FLOAT, updated_at FLOAT)",
    "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
    "kind VARCHAR(16) NOT NULL DEFAULT 'chat', title VARCHAR(256), "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE)",
    "CREATE TABLE channels (id VARCHAR(36) PRIMARY KEY, profile VARCHAR(128) NOT NULL, "
    "channel_type VARCHAR(32) NOT NULL, mode VARCHAR(16) NOT NULL DEFAULT 'bot', "
    "auth_mode VARCHAR(16) NOT NULL DEFAULT 'none', "
    "response_mode VARCHAR(16) NOT NULL DEFAULT 'normal', "
    "enabled BOOLEAN NOT NULL DEFAULT 1, config JSON, state JSON, "
    "created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(profile) REFERENCES profiles(name) ON DELETE CASCADE, "
    "CONSTRAINT uq_channels_profile_type UNIQUE (profile, channel_type))",
    # channel_senders at the prior head: phone + wa_lid present, no send_confirmation.
    "CREATE TABLE channel_senders (id VARCHAR(36) PRIMARY KEY, "
    "channel_id VARCHAR(36) NOT NULL, sender_id VARCHAR(256) NOT NULL, "
    "display_name VARCHAR(256), phone VARCHAR(32), wa_lid VARCHAR(64), "
    "authenticated BOOLEAN NOT NULL DEFAULT 0, "
    "pending_otp VARCHAR(16), pending_otp_expires_at FLOAT, "
    "conversation_id VARCHAR(128), created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, "
    "FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE, "
    "FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL, "
    "CONSTRAINT uq_channel_senders UNIQUE (channel_id, sender_id))",
    "CREATE INDEX ix_channel_senders_channel_id ON channel_senders (channel_id)",
    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
)

_SEED = (
    "INSERT INTO profiles VALUES ('pid','p1',0,0)",
    "INSERT INTO conversations (id,profile,kind,title,created_at,updated_at) "
    "VALUES ('conv1','p1','chat','Chat',0,0)",
    "INSERT INTO channels (id,profile,channel_type,mode,created_at,updated_at) "
    "VALUES ('ch1','p1','whatsapp','bot',0,0)",
    "INSERT INTO channel_senders "
    "(id,channel_id,sender_id,display_name,phone,authenticated,conversation_id,"
    "created_at,updated_at) "
    "VALUES ('s1','ch1','84901234567@s.whatsapp.net','Lee','84901234567',1,'conv1',0,0)",
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


def test_send_confirmation_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)

    _build_old_db(provider)
    mig.upgrade("head")
    mig.upgrade("head")  # idempotent re-run

    eng = provider.sync_engine()
    with eng.connect() as c:
        insp = inspect(c)
        cols = {x["name"]: x for x in insp.get_columns("channel_senders")}
        assert cols["send_confirmation"]["nullable"] is True

        assert "ix_channel_senders_channel_id" in {
            i["name"] for i in insp.get_indexes("channel_senders")
        }

        # Existing clients must read as "inherit", i.e. behave exactly as before.
        row = c.execute(text(
            "SELECT sender_id, phone, authenticated, conversation_id, "
            "send_confirmation FROM channel_senders WHERE id='s1'"
        )).one()
        assert row.sender_id == "84901234567@s.whatsapp.net"
        assert row.phone == "84901234567"
        assert bool(row.authenticated) is True
        assert row.conversation_id == "conv1"
        assert row.send_confirmation is None

    with eng.begin() as c:
        with pytest.raises(IntegrityError):
            c.execute(text(
                "INSERT INTO channel_senders "
                "(id,channel_id,sender_id,authenticated,created_at,updated_at) "
                "VALUES ('s2','ch1','84901234567@s.whatsapp.net',0,0,0)"
            ))


def test_send_confirmation_downgrade_removes_the_column(tmp_path: Path, monkeypatch) -> None:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))
    mig = _patch(monkeypatch, provider)

    _build_old_db(provider)
    mig.upgrade("head")
    mig.downgrade(_PRIOR_HEAD)

    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        cols = {x["name"] for x in insp.get_columns("channel_senders")}
        assert "send_confirmation" not in cols
        # phone/wa_lid from the previous migration must survive the rebuild.
        assert {"phone", "wa_lid"} <= cols
        assert "ix_channel_senders_channel_id" in {
            i["name"] for i in insp.get_indexes("channel_senders")
        }
