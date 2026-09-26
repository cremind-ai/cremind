"""Backend-neutral scenarios for the search-tool rename migrations.

Shared by ``test_search_tool_ids_migration.py`` (SQLite, always runs) and
``test_search_tool_ids_migration_pg.py`` (PostgreSQL, runs when a throwaway
database is configured). Every scenario starts from an EMPTY database driven
through the real Alembic chain to an older head — the same path a real install
took — then seeds it the way that release wrote its rows, upgrades to head and
checks that every profile's state landed where it belongs.

Three starting points:

- **v0.0.18** (``20260829_channel_groups``, the released head): only the
  system tool ``documentation_search`` (Cremind's manual search) exists.
- **dev** (``20260927_userdocs_research``): the unreleased stack, with BOTH
  ``documentation_search`` and ``user_documents`` and every ``userdoc_*``
  table.
- **dev, booted on the renamed code** before these migrations existed: the
  registry already inserted a fresh ``cremind_documentation_search`` row with
  defaults, and a few usage rows were written under the new names.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text

V0018_HEAD = "20260829_channel_groups"
DEV_HEAD = "20260927_userdocs_research"
HEAD = "20260929_profile_working_dir"

T = 1_790_000_000_000.0

PROFILES = ("alice", "bob")

# (scope, key, value, is_secret) per profile.
MANUAL_CONFIG: dict[str, list[tuple[str, str, str, bool]]] = {
    "alice": [
        ("arg", "max_results", "5", False),
        ("leaf", "read_documentation_section", "false", False),
        ("variable", "DEFAULT_TOP_K", "7", False),
    ],
    "bob": [
        ("meta", "note", "bob-manual", False),
        ("variable", "DEFAULT_TOP_K", "3", False),
    ],
}
MANUAL_MEMBERSHIP = {"alice": True, "bob": False}

PERSONAL_CONFIG: dict[str, list[tuple[str, str, str, bool]]] = {
    "alice": [
        ("leaf", "research", "false", False),
        ("variable", "RESEARCH_MODEL_GROUP", "high", False),
    ],
    "bob": [
        ("leaf", "find_files", "false", False),
        ("variable", "DRIVE_TOKEN", "s3cret", True),
        ("variable", "RESEARCH_MODEL_GROUP", "low", False),
    ],
}
PERSONAL_MEMBERSHIP = {"alice": False, "bob": True}

# A built-in and a skill that must come through untouched.
CONTROL_TOOLS = (
    ("web_search", "Web Search", "builtin", "web_search"),
    ("alice__notes", "Notes", "skill", "/skills/alice/notes"),
)
CONTROL_CONFIG = {
    "web_search": ("alice", "variable", "ENGINE", "ddg"),
    "alice__notes": ("alice", "variable", "NOTES_DIR", "/n"),
}


# ── low-level helpers ────────────────────────────────────────────────────────


def ex(conn, sql: str, **params: Any) -> Any:
    """Execute ``sql``; dict/list params are bound as JSON (portable)."""
    stmt = text(sql)
    json_keys = [k for k, v in params.items() if isinstance(v, (dict, list))]
    if json_keys:
        stmt = stmt.bindparams(*(sa.bindparam(k, type_=sa.JSON) for k in json_keys))
    return conn.execute(stmt, params)


def as_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def drop_new_conversation_columns(engine) -> None:
    """The baseline revision builds ``conversations`` from the LIVE ORM, so an
    empty DB upgraded to an old head already has today's columns. A real old
    install does not — drop them so the upgrade really adds them."""
    with engine.begin() as c:
        cols = {col["name"] for col in sa.inspect(c).get_columns("conversations")}
        for name in ("search_cache_baseline", "search_tools_version", "search_tools"):
            if name in cols:
                c.execute(text(f"ALTER TABLE conversations DROP COLUMN {name}"))


def tables(engine) -> set[str]:
    with engine.connect() as c:
        return set(sa.inspect(c).get_table_names())


def version(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


# ── seeding ──────────────────────────────────────────────────────────────────


def _tool(c, tool_id: str, name: str, tool_type: str, source: str) -> None:
    ex(c, "INSERT INTO tools (tool_id,name,tool_type,source,description,created_at,updated_at) "
          "VALUES (:i,:n,:t,:s,'d',:ts,:ts)", i=tool_id, n=name, t=tool_type, s=source, ts=T)


def _config(c, tool_id: str, config: dict, membership: dict) -> None:
    for profile, rows in config.items():
        for scope, key, value, secret in rows:
            ex(c, "INSERT INTO tool_configs (profile,tool_id,scope,key,value,is_secret,updated_at) "
                  "VALUES (:p,:t,:sc,:k,:v,:s,:ts)",
               p=profile, t=tool_id, sc=scope, k=key, v=value, s=secret, ts=T)
    for profile, enabled in membership.items():
        ex(c, "INSERT INTO profile_tools (profile,tool_id,enabled,added_at) VALUES (:p,:t,:e,:ts)",
           p=profile, t=tool_id, e=enabled, ts=T)


def _usage(c, uid: str, profile: str, tool_id: str | None, kind: str, tokens: int,
           conversation_id: str | None = None) -> None:
    ex(c, "INSERT INTO usage_records (id,conversation_id,profile,source_kind,tool_id,step_index,"
          "input_tokens,cache_read_input_tokens,cache_creation_input_tokens,output_tokens,created_at) "
          "VALUES (:i,:c,:p,:k,:t,0,:n,0,0,1,:ts)",
       i=uid, c=conversation_id, p=profile, k=kind, t=tool_id, n=tokens, ts=T)


def seed_common(c) -> None:
    """Profiles, a conversation each (with messages), the manual search, the
    control tools, usage rows and an unrelated server setting."""
    for i, name in enumerate(PROFILES):
        ex(c, "INSERT INTO profiles (id,name,created_at,updated_at) VALUES (:i,:n,:ts,:ts)",
           i=f"uuid-{name}-{i}", n=name, ts=T)
    for name in PROFILES:
        ex(c, "INSERT INTO conversations (id,profile,title,created_at,updated_at) "
              "VALUES (:i,:p,'t',:ts,:ts)", i=f"c-{name}", p=name, ts=T)
        for n, role in enumerate(("user", "agent")):
            ex(c, "INSERT INTO messages (id,conversation_id,role,content,created_at,ordering) "
                  "VALUES (:i,:c,:r,'hi',:ts,:o)", i=f"m-{name}-{n}", c=f"c-{name}", r=role, ts=T, o=n)

    _tool(c, "documentation_search", "Documentation Search", "builtin", "documentation_search")
    _config(c, "documentation_search", MANUAL_CONFIG, MANUAL_MEMBERSHIP)
    for tool_id, name, tool_type, source in CONTROL_TOOLS:
        _tool(c, tool_id, name, tool_type, source)
        profile, scope, key, value = CONTROL_CONFIG[tool_id]
        ex(c, "INSERT INTO tool_configs (profile,tool_id,scope,key,value,is_secret,updated_at) "
              "VALUES (:p,:t,:sc,:k,:v,:s,:ts)", p=profile, t=tool_id, sc=scope, k=key, v=value,
           s=False, ts=T)

    _usage(c, "u1", "alice", "documentation_search", "tool", 10, "c-alice")
    _usage(c, "u2", "bob", "documentation_search", "tool", 20, "c-bob")
    _usage(c, "u3", "alice", "web_search", "tool", 5, "c-alice")
    _usage(c, "u4", "alice", None, "reasoning", 40, "c-alice")
    ex(c, "INSERT INTO server_config (key,value,is_secret,updated_at) VALUES ('other.key','x',:s,:ts)",
       s=False, ts=T)


def seed_dev(c) -> None:
    """Everything the unreleased User Document Search stack wrote."""
    _tool(c, "user_documents", "User Documents", "builtin", "user_documents")
    _config(c, "user_documents", PERSONAL_CONFIG, PERSONAL_MEMBERSHIP)
    _usage(c, "u5", "alice", "user_documents", "userdocs", 100, "c-alice")
    _usage(c, "u6", "bob", "user_documents", "userdocs", 200, None)

    for key, value in (("userdocs.allowed", "true"), ("userdocs.workers", "3"),
                       ("userdocs.max_file_mb", "64"),
                       ("documentation_search.max_file_mb", "128")):
        ex(c, "INSERT INTO server_config (key,value,is_secret,updated_at) VALUES (:k,:v,:s,:ts)",
           k=key, v=value, s=False, ts=T)

    for row in SOURCES:
        ex(c, "INSERT INTO userdoc_sources (id,profile,kind,enabled,root_mode,root_path,excludes,"
              "options,first_sync_confirmed_at,created_at,updated_at) "
              "VALUES (:id,:profile,:kind,:enabled,:root_mode,:root_path,:excludes,:options,"
              ":first_sync_confirmed_at,:ts,:ts)", ts=T, **row)
    for row in CAPTIONS:
        ex(c, "INSERT INTO userdoc_captions (profile,sha256,variant,caption_json,caption_text,"
              "provider,model,prompt_version,tokens_in,tokens_out,created_at) "
              "VALUES (:profile,:sha256,:variant,:caption_json,:caption_text,:provider,:model,"
              ":prompt_version,:tokens_in,:tokens_out,:ts)", ts=T, **row)
    for row in VISION:
        ex(c, "INSERT INTO userdoc_vision_usage (profile,day,captions,ocr_pages,tokens) "
              "VALUES (:profile,:day,:captions,:ocr_pages,:tokens)", **row)
    for row in CITATIONS:
        ex(c, "INSERT INTO userdoc_citations (id,profile,conversation_id,token,cite_id,target,ref_id,"
              "text_hash,source_kind,locator,label,rel_path,snippet,leaf,web_link,issued_at) "
              "VALUES (:id,:profile,:conversation_id,:token,:cite_id,:target,:ref_id,:text_hash,"
              ":source_kind,:locator,:label,:rel_path,:snippet,:leaf,:web_link,:issued_at)", **row)
    for row in JOBS:
        ex(c, "INSERT INTO userdoc_research_jobs (id,profile,conversation_id,run_id,status,phase,mode,"
              "domain,question,scope,answers,dossier,model_group,tokens_in,tokens_out,budget,"
              "elapsed_s,rev,delivered_rev,created_at,updated_at) "
              "VALUES (:id,:profile,:conversation_id,:run_id,:status,:phase,:mode,:domain,:question,"
              ":scope,:answers,:dossier,:model_group,:tokens_in,:tokens_out,:budget,:elapsed_s,"
              ":rev,:delivered_rev,:ts,:ts)", ts=T, **row)


def seed_renamed_boot(c) -> None:
    """What booting the renamed code on an un-migrated dev DB left behind."""
    _tool(c, "cremind_documentation_search", "Cremind Documentation Search", "builtin",
          "cremind_documentation_search")
    _config(c, "cremind_documentation_search",
            {"alice": [("variable", "DEFAULT_TOP_K", "99", False)]}, {"alice": False})
    # The personal search's own usage, already under its new id and kind.
    _usage(c, "u7", "alice", "documentation_search", "documents", 50, "c-alice")
    _usage(c, "u8", "bob", "cremind_documentation_search", "tool", 7, "c-bob")


SOURCES = [
    dict(id="src-a-local", profile="alice", kind="local", enabled=True, root_mode="custom",
         root_path="/home/a/docs",
         excludes=[{"pattern": "*.tmp", "type": "glob", "mode": "skip"}],
         options={"allow_in": {"web_cli": True, "channels": False, "rooms": True}, "caption": True},
         first_sync_confirmed_at=123.0),
    dict(id="src-a-drive", profile="alice", kind="drive", enabled=False, root_mode="inherit",
         root_path=None, excludes=None, options={"include_folders": ["f1", "f2"]},
         first_sync_confirmed_at=None),
    dict(id="src-b-local", profile="bob", kind="local", enabled=True, root_mode="inherit",
         root_path="/srv/b", excludes=None, options={"allow_in": {"web_cli": True}},
         first_sync_confirmed_at=None),
]
CAPTIONS = [
    dict(profile="alice", sha256="h1", variant="image", caption_json={"objects": ["dog"]},
         caption_text="a dog", provider="openai", model="gpt", prompt_version=2,
         tokens_in=11, tokens_out=12),
    dict(profile="bob", sha256="h2", variant="ocr", caption_json=None, caption_text="page text",
         provider=None, model=None, prompt_version=1, tokens_in=0, tokens_out=0),
]
VISION = [
    dict(profile="alice", day="2026-09-25", captions=3, ocr_pages=1, tokens=500),
    dict(profile="bob", day="2026-09-25", captions=1, ocr_pages=0, tokens=50),
]


def _cit(id, profile, conv, token, cite_id, **kw):
    row = dict(id=id, profile=profile, conversation_id=conv, token=token, cite_id=cite_id,
               target="file", ref_id=1, text_hash="th", source_kind="local", locator=None,
               label="L", rel_path="a.txt", snippet="s", leaf="search", web_link=None, issued_at=1.0)
    row.update(kw)
    return row


CITATIONS = [
    _cit("cit-1", "alice", "c-alice", "[ud:k7m2xq9a]", "k7m2xq9a", locator={"page": 2},
         label="Report", rel_path="docs/report.pdf"),
    _cit("cit-2", "alice", None, "[ud:abcd2345#c0ffee12]", "abcd2345", source_kind="drive",
         leaf="read", web_link="https://drive.example/x"),
    _cit("cit-3", "bob", "c-bob", "[ud:zzzz9999]", "zzzz9999", target="folder", ref_id=None,
         leaf="find_files"),
    # Issued by the renamed code already: stays as it is.
    _cit("cit-4", "alice", "c-alice", "[doc:mmmm2222]", "mmmm2222"),
    # A legacy token whose canonical twin already exists in the same
    # conversation: the swap would break UNIQUE(conversation_id, token).
    _cit("cit-5", "alice", "c-alice", "[ud:twin2345]", "twin2345"),
    _cit("cit-6", "alice", "c-alice", "[doc:twin2345]", "twin2345"),
]
EXPECTED_TOKENS_AT_HEAD = {
    "cit-1": "[doc:k7m2xq9a]",
    "cit-2": "[doc:abcd2345#c0ffee12]",
    "cit-3": "[doc:zzzz9999]",
    "cit-4": "[doc:mmmm2222]",
    "cit-5": "[ud:twin2345]",
    "cit-6": "[doc:twin2345]",
}
JOBS = [
    dict(id="job-a1", profile="alice", conversation_id="c-alice", run_id="run-1", status="complete",
         phase="done", mode="analyze", domain="legal", question="Is clause 4 enforceable?",
         scope={"paths": ["contracts/"]}, answers={"q1": "yes"}, dossier={"findings": [1, 2]},
         model_group="high", tokens_in=100, tokens_out=20, budget=1000, elapsed_s=12.5,
         rev=2, delivered_rev=2),
    dict(id="job-b1", profile="bob", conversation_id=None, run_id=None,
         status="needs_clarification", phase="planning", mode="compile", domain="general",
         question="Summarise the folder", scope=None, answers=None, dossier=None,
         model_group=None, tokens_in=0, tokens_out=0, budget=0, elapsed_s=0.0,
         rev=1, delivered_rev=0),
]


# ── reading back ────────────────────────────────────────────────────────────


def tool_rows(c) -> dict[str, tuple[str, str]]:
    return {r[0]: (r[1], r[2]) for r in ex(c, "SELECT tool_id, tool_type, source FROM tools")}


def config_of(c, tool_id: str) -> dict[str, list[tuple[str, str, str, bool]]]:
    out: dict[str, list] = {}
    for r in ex(c, "SELECT profile, scope, key, value, is_secret FROM tool_configs "
                   "WHERE tool_id = :t", t=tool_id):
        out.setdefault(r[0], []).append((r[1], r[2], r[3], bool(r[4])))
    return {p: sorted(rows) for p, rows in out.items()}


def membership_of(c, tool_id: str) -> dict[str, bool]:
    return {r[0]: bool(r[1]) for r in ex(
        c, "SELECT profile, enabled FROM profile_tools WHERE tool_id = :t", t=tool_id)}


def usage_of(c) -> dict[str, tuple[str | None, str, int]]:
    return {r[0]: (r[1], r[2], int(r[3])) for r in ex(
        c, "SELECT id, tool_id, source_kind, input_tokens FROM usage_records")}


def server_config(c) -> dict[str, str]:
    return {r[0]: r[1] for r in ex(c, "SELECT key, value FROM server_config")}


def rows(c, table: str, key: str) -> dict[str, dict[str, Any]]:
    out = {}
    for r in ex(c, f"SELECT * FROM {table}").mappings():
        d = dict(r)
        for col in ("excludes", "options", "caption_json", "locator", "scope", "answers", "dossier",
                    "reference_scope", "state"):
            if col in d:
                d[col] = as_json(d[col])
        if "enabled" in d:
            d["enabled"] = bool(d["enabled"])
        out[d[key]] = d
    return out


def snapshot(engine) -> dict[str, Any]:
    """Every row the migrations touch, for "re-running changes nothing"."""
    with engine.connect() as c:
        present = set(sa.inspect(c).get_table_names())
        snap: dict[str, Any] = {}
        for table in ("tools", "profile_tools", "tool_configs", "usage_records", "server_config",
                      "conversations", "messages", "group_chats", "document_sources",
                      "document_captions", "document_vision_usage", "document_citations",
                      "document_research_jobs"):
            if table in present:
                snap[table] = sorted(
                    json.dumps(dict(r), sort_keys=True, default=str)
                    for r in ex(c, f"SELECT * FROM {table}").mappings()
                )
        snap["_version"] = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return snap


# ── schema names ────────────────────────────────────────────────────────────


DOCUMENT_TABLES = ("document_sources", "document_captions", "document_vision_usage",
                   "document_citations", "document_research_jobs")
LEGACY_TABLES = tuple(t.replace("document_", "userdoc_") for t in DOCUMENT_TABLES)


def orm_names(table: str) -> tuple[set[str], set[str]]:
    """(index names, unique-constraint names) the ORM declares for ``table``."""
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401

    t = Base.metadata.tables[table]
    idx = {i.name for i in t.indexes}
    uq = {c.name for c in t.constraints if isinstance(c, sa.UniqueConstraint) and c.name}
    return idx, uq


def db_names(c, table: str) -> tuple[set[str], set[str]]:
    insp = sa.inspect(c)
    idx = {i["name"] for i in insp.get_indexes(table) if not i.get("duplicates_constraint")}
    uq = {u["name"] for u in insp.get_unique_constraints(table) if u.get("name")}
    return idx, uq


def assert_document_schema(engine, *, legacy: bool = False) -> None:
    """Tables and every index / UNIQUE name match the ORM (or, ``legacy``,
    the historical ``userdoc_*`` names); on PostgreSQL the PK / FK names too."""
    with engine.connect() as c:
        present = set(sa.inspect(c).get_table_names())
        want, gone = (LEGACY_TABLES, DOCUMENT_TABLES) if legacy else (DOCUMENT_TABLES, LEGACY_TABLES)
        assert set(want) <= present, set(want) - present
        assert not (set(gone) & present), set(gone) & present
        for table in DOCUMENT_TABLES:
            orm_idx, orm_uq = orm_names(table)
            name = table.replace("document_", "userdoc_") if legacy else table
            if legacy:
                orm_idx = {n.replace("document_", "userdoc_") for n in orm_idx}
                orm_uq = {n.replace("document_", "userdoc_") for n in orm_uq}
            idx, uq = db_names(c, name)
            assert idx == orm_idx, (name, idx, orm_idx)
            assert uq == orm_uq, (name, uq, orm_uq)
            if c.dialect.name == "postgresql":
                insp = sa.inspect(c)
                assert insp.get_pk_constraint(name)["name"] == f"{name}_pkey"
                for fk in insp.get_foreign_keys(name):
                    assert fk["name"].startswith(f"{name}_"), fk["name"]


# ── expectations ────────────────────────────────────────────────────────────


def assert_tools_after_swap(c, *, with_personal: bool) -> None:
    tools = tool_rows(c)
    assert tools["cremind_documentation_search"] == ("builtin", "cremind_documentation_search")
    assert config_of(c, "cremind_documentation_search") == {
        p: sorted(rows) for p, rows in MANUAL_CONFIG.items()
    }
    assert membership_of(c, "cremind_documentation_search") == MANUAL_MEMBERSHIP
    assert "user_documents" not in tools
    assert config_of(c, "user_documents") == {} and membership_of(c, "user_documents") == {}
    if with_personal:
        assert tools["documentation_search"] == ("builtin", "documentation_search")
        assert config_of(c, "documentation_search") == {
            p: sorted(rows) for p, rows in PERSONAL_CONFIG.items()
        }
        assert membership_of(c, "documentation_search") == PERSONAL_MEMBERSHIP
    else:
        assert "documentation_search" not in tools
        assert config_of(c, "documentation_search") == {}
        assert membership_of(c, "documentation_search") == {}
    # The control rows are exactly as seeded.
    for tool_id, name, tool_type, source in CONTROL_TOOLS:
        assert tools[tool_id] == (tool_type, source)
        profile, scope, key, value = CONTROL_CONFIG[tool_id]
        assert config_of(c, tool_id) == {profile: [(scope, key, value, False)]}


def assert_conversations_upgraded(c) -> None:
    cols = {col["name"] for col in sa.inspect(c).get_columns("conversations")}
    assert {"search_tools", "search_tools_version", "search_cache_baseline"} <= cols
    got = {r[0]: (r[1], r[2], r[3]) for r in ex(
        c, "SELECT id, search_tools, search_tools_version, search_cache_baseline FROM conversations")}
    assert got == {f"c-{p}": (None, 0, None) for p in PROFILES}
    # Nothing hanging off a conversation went anywhere.
    assert ex(c, "SELECT COUNT(*) FROM messages").scalar() == 2 * len(PROFILES)
    gcols = {col["name"] for col in sa.inspect(c).get_columns("group_chats")}
    assert {"search_tools", "search_tools_version"} <= gcols


def assert_dev_data_at_head(c) -> None:
    got = rows(c, "document_sources", "id")
    assert set(got) == {r["id"] for r in SOURCES}
    for want in SOURCES:
        row = got[want["id"]]
        for col, value in want.items():
            assert row[col] == value, (want["id"], col, row[col], value)
    caps = rows(c, "document_captions", "sha256")
    for want in CAPTIONS:
        for col, value in want.items():
            assert caps[want["sha256"]][col] == value, (col, caps[want["sha256"]][col], value)
    vis = {(r[0], r[1]): (r[2], r[3], r[4]) for r in ex(
        c, "SELECT profile, day, captions, ocr_pages, tokens FROM document_vision_usage")}
    assert vis == {(v["profile"], v["day"]): (v["captions"], v["ocr_pages"], v["tokens"]) for v in VISION}
    cits = rows(c, "document_citations", "id")
    assert {k: v["token"] for k, v in cits.items()} == EXPECTED_TOKENS_AT_HEAD
    for want in CITATIONS:
        row = cits[want["id"]]
        for col, value in want.items():
            if col != "token":
                assert row[col] == value, (want["id"], col, row[col], value)
    jobs = rows(c, "document_research_jobs", "id")
    for want in JOBS:
        for col, value in want.items():
            assert jobs[want["id"]][col] == value, (want["id"], col, jobs[want["id"]][col], value)

    cfg = server_config(c)
    assert cfg == {
        "other.key": "x",
        "documentation_search.allowed": "true",
        "documentation_search.workers": "3",
        # The key the renamed code had already written wins; the legacy one goes.
        "documentation_search.max_file_mb": "128",
    }
