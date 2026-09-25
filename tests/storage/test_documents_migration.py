"""Migration 20260925_userdocs on a real pre-feature install, plus its storage.

Three purely additive tables. What is worth pinning:

- ``UNIQUE(profile, kind)`` on ``document_sources`` — it is what makes "turn it
  on" an idempotent upsert, and what lets two concurrent enables settle on one
  row instead of two;
- every table cascades with its profile, so deleting a profile leaves no
  settings, cached captions or quota counters behind;
- the daily vision cap is enforced by the database, not by a read-then-write,
  so the last slot of the day cannot be taken twice.

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
from app.storage.documents_storage import DocumentsStorage  # noqa: E402

_PRIOR_HEAD = "20260829_channel_groups"
_NEW_HEAD = "20260925_userdocs"
_NEW_TABLES = ("document_sources", "document_captions", "document_vision_usage")


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT)"
        ))
        c.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        ))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        c.execute(text("INSERT INTO profiles VALUES ('p1','alice',0,0)"))
        c.execute(text("INSERT INTO profiles VALUES ('p2','bob',0,0)"))


def _upgraded(tmp_path: Path, monkeypatch) -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade(_NEW_HEAD)
    mig.upgrade(_NEW_HEAD)  # idempotent re-run: the guards must swallow this
    return provider


def test_documents_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert set(_NEW_TABLES) <= set(insp.get_table_names())
        assert "ix_document_sources_profile" in {
            i["name"] for i in insp.get_indexes("document_sources")
        }
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _NEW_HEAD


def test_one_source_row_per_profile_and_kind(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with pytest.raises(IntegrityError):
        with provider.sync_engine().begin() as c:
            for i in (1, 2):
                c.execute(text(
                    "INSERT INTO document_sources (id,profile,kind,created_at,updated_at) "
                    f"VALUES ('s{i}','alice','local',0,0)"
                ))


def test_upsert_is_idempotent_and_keeps_profiles_apart(tmp_path: Path, monkeypatch) -> None:
    storage = DocumentsStorage(_upgraded(tmp_path, monkeypatch))
    first = storage.upsert_source("alice", "local", enabled=True, root_path="/a")
    again = storage.upsert_source("alice", "local", root_path="/b")
    assert first["id"] == again["id"]
    assert again["enabled"] is True  # untouched field survives a partial update
    assert again["root_path"] == "/b"
    assert storage.get_source("bob", "local") is None
    assert [r["profile"] for r in storage.list_sources()] == ["alice"]
    # Ownership columns are not caller-settable, so a source cannot be moved
    # to another profile through an update.
    with pytest.raises(ValueError):
        storage.upsert_source("alice", "local", id="someone-elses")


def test_the_vision_cap_is_enforced_by_the_database(tmp_path: Path, monkeypatch) -> None:
    storage = DocumentsStorage(_upgraded(tmp_path, monkeypatch))
    day = "2026-09-25"
    assert [storage.reserve_vision("alice", day, cap=2) for _ in range(3)] == [True, True, False]
    storage.refund_vision("alice", day)
    assert storage.reserve_vision("alice", day, cap=2) is True
    assert storage.vision_usage("alice", day)["captions"] == 2
    # Another profile's day is its own.
    assert storage.reserve_vision("bob", day, cap=2) is True
    # A new day starts from zero.
    assert storage.reserve_vision("alice", "2026-09-26", cap=2) is True
    # A zero cap means captioning is off.
    assert storage.reserve_vision("alice", "2026-09-27", cap=0) is False


def test_captions_are_content_addressed_per_profile(tmp_path: Path, monkeypatch) -> None:
    storage = DocumentsStorage(_upgraded(tmp_path, monkeypatch))
    storage.put_caption("alice", "h1", variant="image", caption_text="two puppies")
    storage.put_caption("alice", "h1", variant="image", caption_text="two puppies in a park")
    assert storage.get_caption("alice", "h1")["caption_text"] == "two puppies in a park"
    assert storage.get_caption("bob", "h1") is None
    assert storage.delete_captions("alice") == 1


def test_deleting_a_profile_cascades(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    storage = DocumentsStorage(provider)
    storage.upsert_source("alice", "local", enabled=True)
    storage.put_caption("alice", "h1", variant="image", caption_text="x")
    storage.reserve_vision("alice", "2026-09-25", cap=5)
    with provider.sync_engine().begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
        c.execute(text("DELETE FROM profiles WHERE name='alice'"))
    with provider.sync_engine().connect() as c:
        for table in _NEW_TABLES:
            n = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE profile='alice'")).scalar()
            assert n == 0, f"{table} kept rows of a deleted profile"
