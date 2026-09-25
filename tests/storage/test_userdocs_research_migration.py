"""Migration 20260927_userdocs_research on an install at 20260926_userdocs_citations.

One additive table. What is worth pinning:

- the upgrade runs from a real older database, and a re-run is a no-op (the
  guards swallow a partial earlier run);
- the counters default to 0 at the DB level, so a row inserted by hand (or by
  a future raw-SQL path) starts undelivered-and-unchanged, not NULL;
- both cascades: deleting a profile or a conversation deletes its jobs, and a
  job with no conversation (REST/CLI) survives a conversation delete;
- the downgrade removes exactly the table.

PostgreSQL takes the same DDL but is not exercised here; per CLAUDE.md that
branch is verified manually against a real PG instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import inspect, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_PRIOR_HEAD = "20260926_userdocs_citations"
_NEW_HEAD = "20260927_userdocs_research"


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


def _job(c, jid, profile, conv):
    c.execute(text(
        "INSERT INTO userdoc_research_jobs (id,profile,conversation_id,status,mode,domain,question,"
        "created_at,updated_at) VALUES (:i,:p,:c,'complete','analyze','general','q?',0,0)"
    ), {"i": jid, "p": profile, "c": conv})


def _count(provider, where="1=1") -> int:
    with provider.sync_engine().connect() as c:
        return int(c.execute(text(f"SELECT COUNT(*) FROM userdoc_research_jobs WHERE {where}")).scalar())


def test_upgrade_creates_the_jobs_table(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with provider.sync_engine().connect() as c:
        insp = inspect(c)
        assert "userdoc_research_jobs" in insp.get_table_names()
        cols = {col["name"]: col for col in insp.get_columns("userdoc_research_jobs")}
        assert set(cols) == {
            "id", "profile", "conversation_id", "run_id", "status", "phase", "mode", "domain",
            "question", "scope", "reference_scope", "answers", "state", "dossier", "model_group",
            "provider", "model", "tokens_in", "tokens_out", "budget", "elapsed_s", "rev",
            "delivered_rev", "error", "created_at", "updated_at", "finished_at",
        }
        assert cols["conversation_id"]["nullable"] and cols["finished_at"]["nullable"]
        assert not cols["question"]["nullable"] and not cols["rev"]["nullable"]
        assert {"ix_userdoc_research_jobs_profile_created", "ix_userdoc_research_jobs_conversation"} <= {
            i["name"] for i in insp.get_indexes("userdoc_research_jobs")
        }
        fks = {tuple(fk["constrained_columns"]): fk for fk in insp.get_foreign_keys("userdoc_research_jobs")}
        assert fks[("profile",)]["referred_table"] == "profiles"
        assert fks[("conversation_id",)]["referred_table"] == "conversations"
        assert all(fk["options"].get("ondelete") == "CASCADE" for fk in fks.values())
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _NEW_HEAD


def test_counters_default_to_zero(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with provider.sync_engine().begin() as c:
        _job(c, "j1", "alice", "c1")
    with provider.sync_engine().connect() as c:
        row = c.execute(text(
            "SELECT rev, delivered_rev, tokens_in, tokens_out, budget, elapsed_s "
            "FROM userdoc_research_jobs WHERE id='j1'"
        )).fetchone()
    assert tuple(row) == (0, 0, 0, 0, 0, 0)


def test_cascades_with_the_profile_and_the_conversation(tmp_path: Path, monkeypatch) -> None:
    provider = _upgraded(tmp_path, monkeypatch)
    with provider.sync_engine().begin() as c:
        _job(c, "j1", "alice", "c1")
        _job(c, "j2", "alice", "c2")
        _job(c, "j3", "alice", None)  # started over REST: no conversation
        _job(c, "j4", "bob", "c3")
    with provider.sync_engine().begin() as c:
        c.execute(text("DELETE FROM conversations WHERE id='c1'"))
    assert _count(provider, "id='j1'") == 0
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
        assert "userdoc_research_jobs" not in tables
        assert {"profiles", "conversations"} <= tables
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == _PRIOR_HEAD
