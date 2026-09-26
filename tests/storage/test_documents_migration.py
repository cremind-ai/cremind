"""Migration 20260925_userdocs on a real pre-feature install, plus its storage.

The revision is historical: it creates ``userdoc_sources`` /
``userdoc_captions`` / ``userdoc_vision_usage``, and a later revision
(``20260928b_document_tables``) renames them to ``document_*``. So the
revision itself is checked at its own head, against the names it really
creates, and everything else runs on the same old install carried on to head —
the path a real install takes. What is worth pinning:

- ``UNIQUE(profile, kind)`` on the sources table — it is what makes "turn it
  on" an idempotent upsert, and what lets two concurrent enables settle on one
  row instead of two — both as created and after the rename's rebuild;
- every table cascades with its profile, so deleting a profile leaves no
  settings, cached captions or quota counters behind;
- the daily vision cap is enforced by the database, not by a read-then-write,
  so the last slot of the day cannot be taken twice.

PostgreSQL takes the same DDL; the rename is exercised on PostgreSQL by
``test_search_tool_ids_migration_pg.py`` when a throwaway database is set up.
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
_REVISION = "20260925_userdocs"
_HISTORICAL_TABLES = ("userdoc_sources", "userdoc_captions", "userdoc_vision_usage")
_TABLES = ("document_sources", "document_captions", "document_vision_usage")


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT)"
        ))
        # Every real install has one, and the later revisions on the way to
        # head add tables that reference it: with foreign keys on, SQLite
        # refuses to delete a profile while an FK names a missing table.
        c.execute(text(
            "CREATE TABLE conversations (id VARCHAR(128) PRIMARY KEY, profile VARCHAR(128) "
            "REFERENCES profiles(name) ON DELETE CASCADE, created_at FLOAT, updated_at FLOAT)"
        ))
        c.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        ))
        c.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": _PRIOR_HEAD})
        c.execute(text("INSERT INTO profiles VALUES ('p1','alice',0,0)"))
        c.execute(text("INSERT INTO profiles VALUES ('p2','bob',0,0)"))


def _upgraded(tmp_path: Path, monkeypatch, target: str = "head") -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade(target)
    mig.upgrade(target)  # idempotent re-run: the guards must swallow this
    return provider


def test_documents_migration_sqlite(tmp_path: Path, monkeypatch) -> None:
    import app.storage.migrations as mig

    provider = _upgraded(tmp_path, monkeypatch, _REVISION)
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert set(_HISTORICAL_TABLES) <= set(insp.get_table_names())
        assert "ix_userdoc_sources_profile" in {
            i["name"] for i in insp.get_indexes("userdoc_sources")
        }
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _REVISION

    # The same install carried on to head: renamed, with the ORM's names.
    mig.upgrade("head")
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        names = set(insp.get_table_names())
        assert set(_TABLES) <= names and not (set(_HISTORICAL_TABLES) & names)
        assert "ix_document_sources_profile" in {
            i["name"] for i in insp.get_indexes("document_sources")
        }
        assert {u["name"] for u in insp.get_unique_constraints("document_sources")} == {
            "uq_document_sources_profile_kind"
        }


@pytest.mark.parametrize("target,table", [
    (_REVISION, "userdoc_sources"),
    ("head", "document_sources"),
])
def test_one_source_row_per_profile_and_kind(tmp_path: Path, monkeypatch, target, table) -> None:
    provider = _upgraded(tmp_path, monkeypatch, target)
    with pytest.raises(IntegrityError):
        with provider.sync_engine().begin() as c:
            for i in (1, 2):
                c.execute(text(
                    f"INSERT INTO {table} (id,profile,kind,created_at,updated_at) "
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
        for table in _TABLES:
            n = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE profile='alice'")).scalar()
            assert n == 0, f"{table} kept rows of a deleted profile"
