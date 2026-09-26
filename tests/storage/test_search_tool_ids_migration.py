"""The search-tool rename migrations, on real older SQLite installs.

Three revisions carry the rename of the two document searches and the new
per-conversation search-tool columns:

- ``20260928_search_tool_ids`` — the built-in ``documentation_search``
  (Cremind's manual search) becomes ``cremind_documentation_search`` FIRST, and
  only then does ``user_documents`` take the freed ``documentation_search``.
  The other order would silently hand every profile's manual-search settings
  to the personal-document search; this suite pins the order with two profiles
  whose settings differ on both tools.
- ``20260928b_document_tables`` — ``userdoc_*`` tables (and every index /
  UNIQUE name), ``userdocs.*`` admin keys, the ``userdocs`` usage kind and the
  ``[ud:`` citation tokens take their new names, data intact.
- ``20260928c_search_tools`` — the additive ``conversations`` /
  ``group_chats`` columns, with every existing conversation reading as the
  default and not one message lost.

Each scenario starts from an empty database driven through the real chain to
an older head (see ``_search_rename_scenarios``). The same scenarios run on
PostgreSQL in ``test_search_tool_ids_migration_pg.py`` when a throwaway
database is configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from tests.storage import _search_rename_scenarios as S  # noqa: E402


@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "cremind.db"))

    import app.databases as dbs
    import app.storage.migrations as mig

    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    return provider, mig


def _at(db, revision: str, *seeders):
    provider, mig = db
    mig.upgrade(revision)
    eng = provider.sync_engine()
    S.drop_new_conversation_columns(eng)
    with eng.begin() as c:
        for seed in seeders:
            seed(c)
    return eng, mig


# ── v0.0.18 → head ──────────────────────────────────────────────────────────


def test_v0018_install_moves_the_manual_search_per_profile(db) -> None:
    eng, mig = _at(db, S.V0018_HEAD, S.seed_common)
    mig.upgrade("head")

    assert S.version(eng) == S.HEAD
    with eng.connect() as c:
        S.assert_tools_after_swap(c, with_personal=False)
        assert S.usage_of(c) == {
            "u1": ("cremind_documentation_search", "tool", 10),
            "u2": ("cremind_documentation_search", "tool", 20),
            "u3": ("web_search", "tool", 5),
            "u4": (None, "reasoning", 40),
        }
        assert S.server_config(c) == {"other.key": "x"}
        S.assert_conversations_upgraded(c)
    S.assert_document_schema(eng)

    # Re-running is a no-op.
    before = S.snapshot(eng)
    mig.upgrade("head")
    assert S.snapshot(eng) == before


def test_a_user_tool_holding_the_new_id_is_moved_aside(db) -> None:
    """An MCP server that happens to be named "Cremind Documentation Search"
    keeps its per-profile state under ``<id>_2``, as the registry would."""

    def seed_mcp(c):
        S._tool(c, "cremind_documentation_search", "Cremind Documentation Search", "mcp",
                "https://mcp.example/docs")
        S._config(c, "cremind_documentation_search",
                  {"bob": [("variable", "MCP_HOST", "h", False)]}, {"bob": True})

    eng, mig = _at(db, S.V0018_HEAD, S.seed_common, seed_mcp)
    mig.upgrade("head")
    with eng.connect() as c:
        S.assert_tools_after_swap(c, with_personal=False)
        tools = S.tool_rows(c)
        assert tools["cremind_documentation_search_2"] == ("mcp", "https://mcp.example/docs")
        assert S.config_of(c, "cremind_documentation_search_2") == {
            "bob": [("variable", "MCP_HOST", "h", False)]
        }
        assert S.membership_of(c, "cremind_documentation_search_2") == {"bob": True}


# ── dev (20260927) → head ───────────────────────────────────────────────────


def test_dev_install_swaps_in_order_and_renames_everything(db) -> None:
    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev)
    mig.upgrade("head")

    assert S.version(eng) == S.HEAD
    with eng.connect() as c:
        S.assert_tools_after_swap(c, with_personal=True)
        assert S.usage_of(c) == {
            "u1": ("cremind_documentation_search", "tool", 10),
            "u2": ("cremind_documentation_search", "tool", 20),
            "u3": ("web_search", "tool", 5),
            "u4": (None, "reasoning", 40),
            "u5": ("documentation_search", "documents", 100),
            "u6": ("documentation_search", "documents", 200),
        }
        S.assert_dev_data_at_head(c)
        S.assert_conversations_upgraded(c)
    S.assert_document_schema(eng)

    before = S.snapshot(eng)
    mig.upgrade("head")
    assert S.snapshot(eng) == before


def test_dev_install_booted_on_the_renamed_code(db) -> None:
    """The fresh ``cremind_documentation_search`` row a renamed boot registered
    loses to the migrated settings; usage already written under the new names
    stays where it is."""
    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev, S.seed_renamed_boot)
    mig.upgrade("head")
    with eng.connect() as c:
        S.assert_tools_after_swap(c, with_personal=True)
        usage = S.usage_of(c)
        assert usage["u7"] == ("documentation_search", "documents", 50)
        assert usage["u8"] == ("cremind_documentation_search", "tool", 7)
        assert usage["u1"] == ("cremind_documentation_search", "tool", 10)
        assert usage["u5"] == ("documentation_search", "documents", 100)


def test_rebuilt_tables_still_enforce_their_constraints(db) -> None:
    """The SQLite rebuild of ``document_sources`` / ``document_citations`` keeps
    the UNIQUE and both cascades."""
    from sqlalchemy.exc import IntegrityError

    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev)
    mig.upgrade("head")
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO document_sources (id,profile,kind,created_at,updated_at) "
                "VALUES ('dup','alice','local',0,0)"
            ))
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text(
                "INSERT INTO document_citations (id,profile,conversation_id,token,cite_id,target,"
                "source_kind,label,rel_path,snippet,leaf,issued_at) VALUES ('dup','alice','c-alice',"
                "'[doc:k7m2xq9a]','k7m2xq9a','file','local','','a','','search',0)"
            ))
    with eng.begin() as c:
        c.execute(text("DELETE FROM conversations WHERE id='c-alice'"))
    with eng.connect() as c:
        left = {r[0] for r in c.execute(text("SELECT id FROM document_citations"))}
        assert left == {"cit-2", "cit-3"}  # c-alice's went; the NULL-conversation one stays
        assert {r[0] for r in c.execute(text("SELECT id FROM document_research_jobs"))} == {"job-b1"}
    with eng.begin() as c:
        c.execute(text("DELETE FROM profiles WHERE name='alice'"))
    with eng.connect() as c:
        for table in S.DOCUMENT_TABLES:
            n = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE profile='alice'")).scalar()
            assert n == 0, table


def test_orphans_the_cascade_missed_do_not_block_the_rebuild(db) -> None:
    """A citation whose conversation vanished while FKs were off would fail the
    FK-enforced copy; it is purged (the CASCADE would have removed it)."""
    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute(
            "INSERT INTO userdoc_citations (id,profile,conversation_id,token,cite_id,target,"
            "source_kind,label,rel_path,snippet,leaf,issued_at) VALUES ('orphan','alice','gone',"
            "'[ud:orphan12]','orphan12','file','local','','a','','search',0)"
        )
        raw.commit()
        cur.close()
    finally:
        raw.close()
    # Fresh connections (foreign keys ON again, as at every real boot), so the
    # migration's copy really is FK-enforced.
    eng.dispose()
    mig.upgrade("head")
    with eng.connect() as c:
        ids = {r[0] for r in c.execute(text("SELECT id FROM document_citations"))}
        assert "orphan" not in ids
        assert ids == {c_["id"] for c_ in S.CITATIONS}


def test_table_rename_recovers_from_a_partial_earlier_run(db) -> None:
    """SQLite commits DDL as it goes, so a crash mid-revision can leave some
    tables renamed (with their old index names) and an empty new table beside
    its legacy one. The re-run finishes the job without losing a row."""
    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev)
    mig.upgrade("20260928_search_tool_ids")
    with eng.begin() as c:
        c.execute(text("ALTER TABLE userdoc_research_jobs RENAME TO document_research_jobs"))
        c.execute(text(
            "CREATE TABLE document_sources (id VARCHAR(36) PRIMARY KEY, profile VARCHAR(128), "
            "kind VARCHAR(16), created_at FLOAT, updated_at FLOAT)"
        ))
    mig.upgrade("head")
    S.assert_document_schema(eng)
    with eng.connect() as c:
        S.assert_dev_data_at_head(c)
        S.assert_tools_after_swap(c, with_personal=True)


# ── fresh install, downgrade ─────────────────────────────────────────────────


def test_fresh_install_matches_the_orm(db) -> None:
    provider, mig = db
    mig.upgrade("head")
    eng = provider.sync_engine()
    assert S.version(eng) == S.HEAD
    S.assert_document_schema(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id,name,created_at,updated_at) VALUES ('p','p',0,0)"))
        c.execute(text(
            "INSERT INTO conversations (id,profile,title,created_at,updated_at) VALUES ('c','p','t',0,0)"
        ))
        c.execute(text("INSERT INTO group_chats (id,name,created_at,updated_at) VALUES ('g','G',0,0)"))
    with eng.connect() as c:
        # The DB-level defaults (raw inserts name none of the new columns).
        assert tuple(c.execute(text(
            "SELECT search_tools, search_tools_version, search_cache_baseline FROM conversations"
        )).one()) == (None, 0, None)
        assert tuple(c.execute(text(
            "SELECT search_tools, search_tools_version FROM group_chats"
        )).one()) == (None, 0)
    before = S.snapshot(eng)
    mig.upgrade("head")
    assert S.snapshot(eng) == before


def test_downgrade_is_the_mirror(db) -> None:
    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev)
    mig.upgrade("head")
    at_head = S.snapshot(eng)

    mig.downgrade(S.DEV_HEAD)
    assert S.version(eng) == S.DEV_HEAD
    S.assert_document_schema(eng, legacy=True)
    with eng.connect() as c:
        tools = S.tool_rows(c)
        assert tools["documentation_search"] == ("builtin", "documentation_search")
        assert tools["user_documents"] == ("builtin", "user_documents")
        assert "cremind_documentation_search" not in tools
        assert S.config_of(c, "documentation_search") == {
            p: sorted(r) for p, r in S.MANUAL_CONFIG.items()
        }
        assert S.config_of(c, "user_documents") == {
            p: sorted(r) for p, r in S.PERSONAL_CONFIG.items()
        }
        tokens = {r[0]: r[1] for r in c.execute(text("SELECT id, token FROM userdoc_citations"))}
        assert tokens["cit-1"] == "[ud:k7m2xq9a]"
        assert tokens["cit-6"] == "[doc:twin2345]"  # its [ud:] twin exists: left alone
        assert {k for k in S.server_config(c) if k != "other.key"} == {
            "userdocs.allowed", "userdocs.workers", "userdocs.max_file_mb",
        }
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(conversations)"))}
        assert "search_tools" not in cols
        assert c.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 4

    mig.upgrade("head")
    again = S.snapshot(eng)
    # tools.updated_at is re-stamped by each move; everything else round-trips.
    for table in at_head:
        if table != "tools":
            assert again[table] == at_head[table], table


def test_moved_usage_rows_carry_their_tools_new_display_name(db) -> None:
    """Usage & Cost shows a row's label first. The manual's old judge rows said
    "Documentation Search" — now the PERSONAL search's name — so the swap
    relabels exactly those default labels on the rows it moves (and the
    personal search's "User Documents" rows), never a label a caption or a
    research job wrote."""

    def seed_labels(c):
        for uid, tool_id, kind, label in (
            ("l1", "documentation_search", "tool", "Documentation Search"),
            ("l2", "user_documents", "tool", "User Documents"),
            ("l3", "user_documents", "userdocs", "Document research: compile"),
            ("l4", "web_search", "tool", "Web Search"),
        ):
            S.ex(c, "INSERT INTO usage_records (id,conversation_id,profile,source_kind,tool_id,label,"
                    "step_index,input_tokens,cache_read_input_tokens,cache_creation_input_tokens,"
                    "output_tokens,created_at) VALUES (:i,NULL,'alice',:k,:t,:l,0,1,0,0,1,0)",
                 i=uid, k=kind, t=tool_id, l=label)

    eng, mig = _at(db, S.DEV_HEAD, S.seed_common, S.seed_dev, seed_labels)
    mig.upgrade("head")

    def labels(c):
        return {r[0]: (r[1], r[2]) for r in S.ex(
            c, "SELECT id, tool_id, label FROM usage_records WHERE id LIKE 'l%'")}

    with eng.connect() as c:
        assert labels(c) == {
            "l1": ("cremind_documentation_search", "Cremind Documentation Search"),
            "l2": ("documentation_search", "Documentation Search"),
            "l3": ("documentation_search", "Document research: compile"),
            "l4": ("web_search", "Web Search"),
        }

    # The downgrade mirrors it, labels included.
    mig.downgrade(S.DEV_HEAD)
    with eng.connect() as c:
        assert labels(c) == {
            "l1": ("documentation_search", "Documentation Search"),
            "l2": ("user_documents", "User Documents"),
            "l3": ("user_documents", "Document research: compile"),
            "l4": ("web_search", "Web Search"),
        }
