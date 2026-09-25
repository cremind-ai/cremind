"""Migration 20260926_userdocs_citations on an install at 20260925_userdocs.

One additive table. What is worth pinning:

- the upgrade runs from a real older database, and a re-run is a no-op (the
  guards swallow a partial earlier run);
- ``UNIQUE(conversation_id, token)`` — re-issuing a token is a no-op, not a
  second row, while a NULL conversation (the A2A first message) never
  collides;
- both cascades: deleting a profile or a conversation deletes its citations,
  and nothing else;
- the downgrade removes exactly the table.

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

_PRIOR_HEAD = "20260925_userdocs"
_NEW_HEAD = "20260926_userdocs_citations"


def _build_old_db(provider: SqliteDatabaseProvider) -> None:
    eng = provider.sync_engine()
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE profiles (id VARCHAR(128), name VARCHAR(128) PRIMARY KEY, "
            "created_at FLOAT, updated_at FLOAT)"
        ))
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
        for cid, profile in (("c1", "alice"), ("c2", "alice"), ("c3", "bob")):
            c.execute(text("INSERT INTO conversations VALUES (:i,:p,0,0)"), {"i": cid, "p": profile})


def _upgraded(tmp_path: Path, monkeypatch) -> SqliteDatabaseProvider:
    provider = SqliteDatabaseProvider(str(tmp_path / "old.db"))

    import app.databases as dbs
    import app.storage.migrations as mig
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)

    _build_old_db(provider)
    mig.upgrade(_NEW_HEAD)
    mig.upgrade(_NEW_HEAD)  # idempotent re-run
    return provider


def _cite(c, rid, profile, conv, token="[ud:k7m2xq9a]"):
    c.execute(text(
        "INSERT INTO userdoc_citations (id,profile,conversation_id,token,cite_id,target,"
        "source_kind,label,rel_path,snippet,leaf,issued_at) "
        "VALUES (:i,:p,:c,:t,'k7m2xq9a','file','local','','a.txt','','search',0)"
    ), {"i": rid, "p": profile, "c": conv, "t": token})


def _count(provider, where="1=1") -> int:
    with provider.sync_engine().connect() as c:
        return int(c.execute(text(f"SELECT COUNT(*) FROM userdoc_citations WHERE {where}")).scalar())


def test_upgrade_creates_the_registry(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert "userdoc_citations" in insp.get_table_names()
        cols = {col["name"]: col for col in insp.get_columns("userdoc_citations")}
        assert set(cols) == {
            "id", "profile", "conversation_id", "token", "cite_id", "target", "ref_id",
            "text_hash", "source_kind", "locator", "label", "rel_path", "snippet", "leaf",
            "web_link", "issued_at",
        }
        assert cols["conversation_id"]["nullable"] and cols["ref_id"]["nullable"]
        assert not cols["token"]["nullable"]
        assert "ix_userdoc_citations_profile_cite" in {
            i["name"] for i in insp.get_indexes("userdoc_citations")
        }
        fks = {tuple(fk["constrained_columns"]): fk for fk in insp.get_foreign_keys("userdoc_citations")}
        assert fks[("profile",)]["referred_table"] == "profiles"
        assert fks[("conversation_id",)]["referred_table"] == "conversations"
        assert all(fk["options"].get("ondelete") == "CASCADE" for fk in fks.values())
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _NEW_HEAD


def test_one_row_per_token_per_conversation(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with pytest.raises(IntegrityError):
        with provider.sync_engine().begin() as c:
            _cite(c, "r1", "alice", "c1")
            _cite(c, "r2", "alice", "c1")
    with provider.sync_engine().begin() as c:
        _cite(c, "r1", "alice", "c1")
        _cite(c, "r2", "alice", "c2")    # the same token in another conversation
        _cite(c, "r3", "alice", None)    # NULLs never collide
        _cite(c, "r4", "alice", None)
    assert _count(provider) == 4


def test_cascades_with_the_profile_and_the_conversation(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with provider.sync_engine().begin() as c:
        _cite(c, "r1", "alice", "c1")
        _cite(c, "r2", "alice", "c2")
        _cite(c, "r3", "alice", None)
        _cite(c, "r4", "bob", "c3")
    with provider.sync_engine().begin() as c:
        c.execute(text("DELETE FROM conversations WHERE id='c1'"))
    assert _count(provider, "id='r1'") == 0
    assert _count(provider) == 3
    with provider.sync_engine().begin() as c:
        c.execute(text("DELETE FROM profiles WHERE name='alice'"))
    assert _count(provider, "profile='alice'") == 0
    assert _count(provider, "profile='bob'") == 1


def test_downgrade_removes_the_table(tmp_path: Path, monkeypatch) -> None:
    import app.storage.migrations as mig

    provider = _upgraded(tmp_path, monkeypatch)
    mig.downgrade(_PRIOR_HEAD)
    with provider.sync_engine().connect() as c:
        tables = set(inspect(c).get_table_names())
        assert "userdoc_citations" not in tables
        assert {"profiles", "conversations"} <= tables
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _PRIOR_HEAD
