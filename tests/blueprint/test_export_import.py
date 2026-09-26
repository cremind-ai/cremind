"""End-to-end export → import parity for the blueprint engine (single install).

Populates a ``src`` profile (persona, changed setting, LLM with an API key,
a built-in tool with a secret variable + disabled leaf, a bundled user skill
with a secret env var and an on-disk token file, a canonical listener), exports
a blueprint, then imports it into a fresh ``dst`` profile and asserts:

- no secret VALUE appears anywhere in the (decompressed) archive
- the skill's ``.env`` and OAuth token file are never bundled
- design lands on ``dst``: settings, LLM model group, persona, tool config +
  leaf toggle, and skill config re-keyed from ``src__<slug>`` to ``dst__<slug>``

The event steps (which need conversation rows) are covered separately by the
pure recompute tests; here we drive the steps that need no conversation FK.
"""

from __future__ import annotations

import asyncio
import json
import tarfile
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.databases import create_database_provider, get_database_provider, set_database_provider
from app.storage import migrations

_PLANTED = {
    "llm": "PLANTED-LLM-KEY-sk-abc123",
    "tool": "PLANTED-TOOL-TOKEN-xyz",
    "skill": "PLANTED-SKILL-KEY-def456",
    "oauth": "PLANTED-OAUTH-REFRESH-TOKEN",
}
_PERSONA = "You are Ollie.\nMARKER-BLUEPRINT-TEST\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.config.settings import BaseConfig

    system_dir = tmp_path / "sys"
    system_dir.mkdir()
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(system_dir))
    monkeypatch.delenv("CREMIND_DB_PROVIDER", raising=False)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir), raising=False)
    monkeypatch.setattr(
        BaseConfig, "SQLITE_DB_PATH", str(system_dir / "storage" / "cremind.db"), raising=False
    )
    set_database_provider(None)
    set_database_provider(create_database_provider())
    migrations.upgrade("head")
    yield system_dir
    set_database_provider(None)


def _populate_src(system_dir: Path) -> None:
    from app.utils.agent_name import write_agent_name
    from app.utils.persona import write_persona_file

    now = time.time()
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id,name,created_at,updated_at) VALUES ('p-src','src',:t,:t)"), {"t": now * 1000})
        c.execute(text("INSERT INTO profiles (id,name,created_at,updated_at) VALUES ('p-dst','dst',:t,:t)"), {"t": now * 1000})

        # changed setting
        c.execute(
            text("INSERT INTO user_config (profile,key,value,updated_at) VALUES ('src','agent.max_steps','300',:t)"),
            {"t": now * 1000},
        )
        # LLM: model group + non-secret auth_method + a secret api_key
        for k, v, sec in [
            ("default_provider", "openai", 0),
            ("model_group.high", "openai/gpt-5.4", 0),
            ("openai.auth_method", "api_key", 0),
            ("openai.api_key", _PLANTED["llm"], 1),
        ]:
            c.execute(
                text("INSERT INTO llm_config (profile,key,value,is_secret,updated_at) VALUES ('src',:k,:v,:s,:t)"),
                {"k": k, "v": v, "s": sec, "t": now * 1000},
            )
        # built-in tool row (browser) + its config
        c.execute(
            text("INSERT INTO tools (tool_id,name,tool_type,source,description,created_at,updated_at) "
                 "VALUES ('browser','Browser','builtin','browser','web',:t,:t)"),
            {"t": now * 1000},
        )
        # Skill tool rows. In production these are created by
        # ``resync_profile_skills`` (registry); the parity test drives the
        # appliers with registry=None, so it registers the rows the way resync
        # would — ``src__my_skill`` for export, ``dst__my_skill`` for import
        # (tool_configs.tool_id FK-references tools.tool_id).
        for tid in ("src__my_skill", "dst__my_skill"):
            c.execute(
                text("INSERT INTO tools (tool_id,name,tool_type,source,description,created_at,updated_at) "
                     "VALUES (:tid,'My Skill','skill',:tid,'cs',:t,:t)"),
                {"tid": tid, "t": now * 1000},
            )
        tool_rows = [
            ("browser", "variable", "BROWSER_HOST", "example.com", 0),
            ("browser", "variable", "BROWSER_TOKEN", _PLANTED["tool"], 1),
            ("browser", "leaf", "screenshot", "false", 0),
            ("src__my_skill", "variable", "MY_HOST", "host.example", 0),
            ("src__my_skill", "variable", "MY_API_KEY", _PLANTED["skill"], 1),
        ]
        for tid, scope, key, val, sec in tool_rows:
            c.execute(
                text("INSERT INTO tool_configs (profile,tool_id,scope,key,value,is_secret,updated_at) "
                     "VALUES ('src',:tid,:sc,:k,:v,:s,:t)"),
                {"tid": tid, "sc": scope, "k": key, "v": val, "s": sec, "t": now * 1000},
            )
        # canonical skill listener autostart row
        skill_scripts = system_dir / "src" / "skills" / "my-skill" / "scripts"
        c.execute(
            text("INSERT INTO autostart_processes (id,profile,command,working_dir,is_pty,created_at) "
                 "VALUES ('a-src','src',:cmd,:wd,0,:t)"),
            {
                "cmd": f'uv run "{skill_scripts / "event_listener.py"}"',
                "wd": str(skill_scripts),
                "t": now,
            },
        )

    # persona + agent name files
    write_persona_file("src", _PERSONA)
    write_agent_name("src", "Ollie")

    # on-disk skill (bundled user skill) with a secret .env + OAuth token file
    skill_dir = system_dir / "src" / "skills" / "my-skill"
    (skill_dir / "scripts" / "app").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: My Skill\n"
        "description: A customer-service helper.\n"
        "metadata:\n"
        "  environment_variables:\n"
        "    - {name: MY_API_KEY, required: true, secret: true, type: string}\n"
        "    - {name: MY_HOST, required: false, secret: false, type: string}\n"
        "  long_running_app:\n"
        "    command: uv run scripts/event_listener.py\n"
        "    description: listener\n"
        "---\n\n# My Skill\nDo customer service.\n",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / ".env").write_text(f"MY_API_KEY={_PLANTED['skill']}\n", encoding="utf-8")
    (skill_dir / "scripts" / ".google_token.json").write_text(
        json.dumps({"refresh_token": _PLANTED["oauth"]}), encoding="utf-8"
    )
    (skill_dir / "scripts" / "app" / "client.py").write_text("print('hi')\n", encoding="utf-8")


def _archive_bytes_and_members(path: Path) -> tuple[bytes, list[str]]:
    chunks: list[bytes] = []
    names: list[str] = []
    with tarfile.open(str(path), mode="r:gz") as tf:
        for m in tf:
            names.append(m.name)
            if m.isreg():
                fh = tf.extractfile(m)
                if fh is not None:
                    chunks.append(fh.read())
    return b"".join(chunks), names


def test_export_checklist_detection(env):
    system_dir = env
    _populate_src(system_dir)

    from app.blueprint.detect import collect_exportable
    from app.config.user_config import resolve_default

    # Add a setting whose stored value equals its default (a Setup-Wizard-style
    # redundant row — must NOT appear as a change) and a key no longer in the
    # schema (kept, flagged unknown).
    default_retries = resolve_default("agent.max_llm_retries")
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO user_config (profile,key,value,updated_at) VALUES ('src','agent.max_llm_retries',:v,:t)"),
            {"v": str(default_retries), "t": time.time() * 1000},
        )
        c.execute(
            text("INSERT INTO user_config (profile,key,value,updated_at) VALUES ('src','nope.gone','whatever',:t)"),
            {"t": time.time() * 1000},
        )

    out = collect_exportable("src")
    comps = out["components"]
    # Only the components we actually customized should be available.
    for key in ("persona", "settings", "llm", "tools", "skills", "listeners"):
        assert comps[key]["available"] is True, f"{key} should be exportable"
    # Two real overrides: the changed setting + the unknown key. The row equal
    # to its default is filtered out.
    assert comps["settings"]["count"] == 2
    items = {it["key"]: it for it in comps["settings"]["items"]}
    assert "agent.max_llm_retries" not in items, "a default-equal row must not appear as a change"
    # The changed setting carries full metadata.
    changed = items["agent.max_steps"]
    assert changed["type"] == "number"
    assert changed["label"]
    assert changed["group"] == "agent"
    assert changed["value"] == 300
    assert changed["default"] == resolve_default("agent.max_steps")
    assert changed["unknown"] is False
    # A key no longer in the schema is flagged unknown (still exportable).
    assert items["nope.gone"]["unknown"] is True
    # The bundled user skill shows up, flagged as needing its secret var.
    skills = comps["skills"]["items"]
    assert any(s["slug"] == "my_skill" and "MY_API_KEY" in s["secret_variables"] for s in skills)
    # Events were not populated in this fixture → not available.
    assert comps["events"]["available"] is False


def test_export_settings_skips_default_equal(env):
    system_dir = env
    _populate_src(system_dir)

    from app.blueprint.engine import ExportOptions, create_blueprint
    from app.config.user_config import resolve_default

    # A redundant row equal to its default alongside the genuinely-changed one.
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO user_config (profile,key,value,updated_at) VALUES ('src','agent.max_llm_retries',:v,:t)"),
            {"v": str(resolve_default("agent.max_llm_retries")), "t": time.time() * 1000},
        )

    # Whole-component export (no explicit selection) drops the default-equal row.
    result = create_blueprint(
        ExportOptions(profile="src", name="allset", components={"settings"})
    )
    doc = _component_doc(result.path, "settings")
    assert set(doc["data"]["values"].keys()) == {"agent.max_steps"}

    # An explicit --settings selection is honoured verbatim, even for a
    # default-equal key (power-user override).
    result2 = create_blueprint(
        ExportOptions(
            profile="src",
            name="explicit",
            components={"settings"},
            setting_keys={"agent.max_llm_retries"},
        )
    )
    doc2 = _component_doc(result2.path, "settings")
    assert set(doc2["data"]["values"].keys()) == {"agent.max_llm_retries"}


def _seed_events(system_dir: Path) -> dict[str, str]:
    """Insert a conversation + 2 schedules + 1 skill event on ``src``.

    Returns a mapping of a human label → row id so tests can select subsets.
    """
    now = time.time()
    ids = {
        "daily": "sch-daily",
        "weekly": "sch-weekly",
        "skillev": "se-1",
    }
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO conversations (id,profile,title,created_at,updated_at) "
                 "VALUES ('conv-src','src','t',:t,:t)"),
            {"t": now * 1000},
        )
        for rid, title, rrule in [
            (ids["daily"], "Daily report", "FREQ=DAILY"),
            (ids["weekly"], "Weekly review", "FREQ=WEEKLY"),
        ]:
            c.execute(
                text("INSERT INTO schedule_event_subscriptions "
                     "(id,conversation_id,profile,title,action,dtstart,rrule,created_at,updated_at) "
                     "VALUES (:id,'conv-src','src',:title,:title,'2026-07-12T09:00:00',:rrule,:t,:t)"),
                {"id": rid, "title": title, "rrule": rrule, "t": now * 1000},
            )
        c.execute(
            text("INSERT INTO skill_event_subscriptions "
                 "(id,conversation_id,profile,skill_name,event_type,action,created_at) "
                 "VALUES (:id,'conv-src','src','My Skill','inbox','do it',:t)"),
            {"id": ids["skillev"], "t": now},
        )
    return ids


def test_export_events_carry_ids(env):
    system_dir = env
    _populate_src(system_dir)
    ids = _seed_events(system_dir)

    from app.blueprint.detect import collect_exportable

    out = collect_exportable("src")
    events = out["components"]["events"]
    assert events["available"] is True
    seen = {
        it["id"]
        for group in events["items"].values()
        for it in group
    }
    assert set(ids.values()) <= seen, "every event item must carry its row id"


def test_export_settings_subset(env):
    system_dir = env
    _populate_src(system_dir)

    from app.blueprint.engine import ExportOptions, create_blueprint

    # Add a second changed setting so the subset genuinely excludes something.
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO user_config (profile,key,value,updated_at) VALUES ('src','compaction.enabled','false',:t)"),
            {"t": time.time() * 1000},
        )

    result = create_blueprint(
        ExportOptions(
            profile="src",
            name="subset",
            components={"settings"},
            setting_keys={"agent.max_steps"},
        )
    )
    doc = _component_doc(result.path, "settings")
    assert set(doc["data"]["values"].keys()) == {"agent.max_steps"}
    assert set(doc["data"]["defaults_at_export"].keys()) == {"agent.max_steps"}
    assert result.manifest.summary()["components"]["settings"]["count"] == 1


def test_export_events_subset(env):
    system_dir = env
    _populate_src(system_dir)
    ids = _seed_events(system_dir)

    from app.blueprint.engine import ExportOptions, create_blueprint

    result = create_blueprint(
        ExportOptions(
            profile="src",
            name="subset-events",
            components={"events"},
            event_ids={ids["daily"]},
        )
    )
    doc = _component_doc(result.path, "events")
    titles = [s["title"] for s in doc["data"]["schedule"]]
    assert titles == ["Daily report"]
    assert doc["data"]["skill_event"] == []


def _component_doc(path: Path, key: str) -> dict:
    with tarfile.open(str(path), mode="r:gz") as tf:
        member = tf.extractfile(f"components/{key}.json")
        assert member is not None, f"components/{key}.json missing from archive"
        return json.loads(member.read().decode("utf-8"))


def test_export_import_parity(env):
    system_dir = env
    _populate_src(system_dir)

    from app.blueprint.engine import ExportOptions, create_blueprint

    result = create_blueprint(
        ExportOptions(
            profile="src",
            name="cs-agent",
            display_name="CS Agent",
            description="Customer service",
            components={"persona", "settings", "llm", "tools", "skills", "listeners"},
        )
    )
    assert result.path.is_file()

    # ── no secret VALUE leaks; no credential files bundled ──
    blob, members = _archive_bytes_and_members(result.path)
    text_blob = blob.decode("utf-8", errors="ignore")
    for label, secret in _PLANTED.items():
        assert secret not in text_blob, f"{label} secret leaked into the archive"
    assert any(n.endswith("skills/my-skill/SKILL.md") for n in members)
    assert any(n.endswith("scripts/app/client.py") for n in members)
    assert not any(".env" in n for n in members)
    assert not any("google_token" in n for n in members)

    # manifest requirements name the secrets (names only)
    req_secret_keys = {
        (r.get("provider") or r.get("tool_id") or r.get("skill"), r.get("field") or r.get("variable"))
        for r in result.manifest.requirements.get("secrets", [])
    }
    assert ("openai", "api_key") in req_secret_keys
    assert ("browser", "BROWSER_TOKEN") in req_secret_keys
    assert ("my_skill", "MY_API_KEY") in req_secret_keys

    # ── import into dst ──
    from app.blueprint.apply import (
        Deps,
        apply_llm,
        apply_persona,
        apply_settings,
        apply_skills,
        apply_tools,
    )
    from app.blueprint.apply import apply_listeners
    from app.blueprint.plan import stage_upload
    from app.storage.dynamic_config_storage import DynamicConfigStorage

    session = stage_upload(result.path, owner="admin")
    session.target_profile = "dst"
    session.save()

    deps = Deps(registry=None, conversation_storage=None, config_storage=DynamicConfigStorage())
    apply_settings(session, {}, deps)
    apply_persona(session, {}, deps)
    apply_llm(session, {"secrets": {"openai.api_key": "NEWKEY"}}, deps)
    apply_tools(session, {"secrets": {"browser": {"BROWSER_TOKEN": "NEWTOK"}}}, deps)
    asyncio.run(apply_skills(session, {"secrets": {"my_skill": {"MY_API_KEY": "NEWSKILLKEY"}}}, deps))
    asyncio.run(apply_listeners(session, {}, deps))

    cs = DynamicConfigStorage()
    ts = _tool_storage()

    # settings
    assert cs.get("user_config", "agent.max_steps", profile="dst") == "300"
    # llm
    assert cs.get("llm_config", "default_provider", profile="dst") == "openai"
    assert cs.get("llm_config", "model_group.high", profile="dst") == "openai/gpt-5.4"
    assert cs.get("llm_config", "openai.auth_method", profile="dst") == "api_key"
    assert cs.get("llm_config", "openai.api_key", profile="dst") == "NEWKEY"
    # persona + agent name
    from app.utils.agent_name import read_agent_name
    from app.utils.persona import read_persona_file

    assert read_persona_file("dst") == _PERSONA
    assert read_agent_name("dst") == "Ollie"
    # tools: non-secret var + leaf toggle + supplied secret
    assert ts.get_config(profile="dst", tool_id="browser", scope="variable", key="BROWSER_HOST") == "example.com"
    assert ts.get_config(profile="dst", tool_id="browser", scope="leaf", key="screenshot") == "false"
    assert ts.get_config(profile="dst", tool_id="browser", scope="variable", key="BROWSER_TOKEN") == "NEWTOK"
    # skill config re-keyed src__ -> dst__
    assert ts.get_config(profile="dst", tool_id="dst__my_skill", scope="variable", key="MY_HOST") == "host.example"
    assert ts.get_config(profile="dst", tool_id="dst__my_skill", scope="variable", key="MY_API_KEY") == "NEWSKILLKEY"
    # skill files installed; secrets NOT copied
    dst_skill = system_dir / "dst" / "skills" / "my-skill"
    assert (dst_skill / "SKILL.md").is_file()
    assert (dst_skill / "scripts" / "app" / "client.py").is_file()
    assert not (dst_skill / "scripts" / ".env").exists()
    assert not (dst_skill / "scripts" / ".google_token.json").exists()
    # listener registered for dst under its own skill dir
    from app.storage import get_autostart_storage

    dst_listeners = get_autostart_storage().list("dst")
    assert len(dst_listeners) == 1
    assert str(system_dir / "dst" / "skills" / "my-skill" / "scripts") in dst_listeners[0]["working_dir"]

    # secret is_secret flag preserved on dst
    eng = get_database_provider().sync_engine()
    with eng.connect() as c:
        is_secret = c.execute(
            text("SELECT is_secret FROM llm_config WHERE profile='dst' AND key='openai.api_key'")
        ).scalar()
        assert bool(is_secret) is True


def _tool_storage():
    from app.storage.tool_storage import get_tool_storage

    return get_tool_storage()


# ── tools component v2: the document-search id swap ──────────────────────────


def _seed_profiles_and_tools(tool_ids: tuple[str, ...]) -> None:
    now = time.time() * 1000
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        for pid, name in (("p-src", "src"), ("p-dst", "dst")):
            c.execute(
                text("INSERT INTO profiles (id,name,created_at,updated_at) VALUES (:i,:n,:t,:t)"),
                {"i": pid, "n": name, "t": now},
            )
        for tid in tool_ids:
            c.execute(
                text("INSERT INTO tools (tool_id,name,tool_type,source,description,created_at,updated_at) "
                     "VALUES (:tid,:tid,'builtin',:tid,'d',:t,:t)"),
                {"tid": tid, "t": now},
            )


def _write_blueprint(path: Path, tools_doc: dict) -> Path:
    """A hand-built archive, as an older build would have written it."""
    import io
    import sys

    from app.blueprint.manifest import BlueprintManifest, ComponentEntry, SourcePaths

    manifest = BlueprintManifest(
        app_version="0.0.18",
        platform=sys.platform,
        source_profile="src",
        source_paths=SourcePaths("", "", "", "/", False),
        name="old",
        components={
            "tools": ComponentEntry(
                tools_doc["version"], "components/tools.json",
                {"count": len(tools_doc["data"]["tools"])},
            ),
        },
    )
    with tarfile.open(str(path), mode="w:gz") as tf:
        for name, payload in (
            ("manifest.json", manifest.to_dict()),
            ("components/tools.json", tools_doc),
        ):
            data = json.dumps(payload).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _entry(tool_id: str, *, variables=None, leaves=(), secrets=(), kind="builtin") -> dict:
    return {
        "tool_id": tool_id,
        "kind": kind,
        "enabled": None,
        "config": {"arg": {}, "meta": {}},
        "variables": dict(variables or {}),
        "secret_variables": list(secrets),
        "disabled_leaves": list(leaves),
    }


def test_export_writes_tools_v2_with_todays_ids(env):
    _seed_profiles_and_tools(("cremind_documentation_search", "documentation_search"))
    ts = _tool_storage()
    ts.set_config(profile="src", tool_id="cremind_documentation_search", scope="variable",
                  key="DEFAULT_TOP_K", value="7")
    ts.set_config(profile="src", tool_id="documentation_search", scope="leaf",
                  key="research", value="false")

    from app.blueprint.engine import ExportOptions, create_blueprint

    result = create_blueprint(ExportOptions(profile="src", name="v2", components={"tools"}))
    doc = _component_doc(result.path, "tools")
    assert doc["version"] == 2
    assert result.manifest.components["tools"].version == 2
    assert result.manifest.min_app_version == "0.0.19"
    assert {t["tool_id"] for t in doc["data"]["tools"]} == {
        "cremind_documentation_search", "documentation_search",
    }

    # A v2 document imports as is: no id is mapped twice.
    from app.blueprint.apply import Deps, apply_tools
    from app.blueprint.plan import stage_upload
    from app.storage.dynamic_config_storage import DynamicConfigStorage

    session = stage_upload(result.path, owner="admin")
    session.target_profile = "dst"
    res = apply_tools(session, {}, Deps(registry=None, conversation_storage=None,
                                        config_storage=DynamicConfigStorage()))
    assert res["warnings"] == []
    assert ts.get_config(profile="dst", tool_id="cremind_documentation_search",
                         scope="variable", key="DEFAULT_TOP_K") == "7"
    assert ts.get_config(profile="dst", tool_id="documentation_search",
                         scope="leaf", key="research") == "false"


def test_importing_a_v1_tools_component_maps_the_swapped_ids_once(env, tmp_path):
    _seed_profiles_and_tools(("cremind_documentation_search", "documentation_search", "browser"))
    archive = _write_blueprint(tmp_path / "old.cremind-blueprint", {
        "component": "tools",
        "version": 1,
        "data": {"tools": [
            # v1: the Cremind manual search.
            _entry("documentation_search", variables={"DEFAULT_TOP_K": "7"},
                   leaves=["read_documentation_section"]),
            # v1: the personal-document search.
            _entry("user_documents", variables={"RESEARCH_MODEL_GROUP": "high"},
                   leaves=["research"], secrets=["DRIVE_TOKEN"]),
            # A built-in this install does not have: skipped, and it must not
            # abort the entries after it.
            _entry("from_a_newer_build", variables={"X": "1"}),
            _entry("browser", variables={"BROWSER_HOST": "example.com"}),
        ]},
    })

    from app.blueprint.apply import Deps, apply_tools
    from app.blueprint.plan import stage_upload
    from app.storage.dynamic_config_storage import DynamicConfigStorage

    session = stage_upload(archive, owner="admin")
    session.target_profile = "dst"
    session.save()

    plan = next(s for s in session.plan if s["key"] == "tools")
    preview = {t["tool_id"]: t for t in plan["preview"]["tools"]}
    assert set(preview) == {
        "cremind_documentation_search", "documentation_search", "from_a_newer_build", "browser",
    }
    assert preview["from_a_newer_build"]["available"] is False
    assert preview["documentation_search"]["available"] is True
    assert any(w["kind"] == "plan" and "from_a_newer_build" in w["message"] for w in session.warnings)
    # The secret requirement names the id the tool has HERE.
    assert {"type": "tool_secrets", "tool_id": "documentation_search",
            "variables": ["DRIVE_TOKEN"]} in plan["requirements"]

    # A client that keyed the secret by the id the old manifest printed still works.
    res = apply_tools(
        session, {"secrets": {"user_documents": {"DRIVE_TOKEN": "tok"}}},
        Deps(registry=None, conversation_storage=None, config_storage=DynamicConfigStorage()),
    )
    ts = _tool_storage()

    def cfg(tool_id, scope, key):
        return ts.get_config(profile="dst", tool_id=tool_id, scope=scope, key=key)

    # Manual-search settings landed on cremind_documentation_search ...
    assert cfg("cremind_documentation_search", "variable", "DEFAULT_TOP_K") == "7"
    assert cfg("cremind_documentation_search", "leaf", "read_documentation_section") == "false"
    assert cfg("cremind_documentation_search", "variable", "RESEARCH_MODEL_GROUP") is None
    # ... and the personal search's on documentation_search — not chained onward.
    assert cfg("documentation_search", "variable", "RESEARCH_MODEL_GROUP") == "high"
    assert cfg("documentation_search", "leaf", "research") == "false"
    assert cfg("documentation_search", "variable", "DRIVE_TOKEN") == "tok"
    assert cfg("documentation_search", "variable", "DEFAULT_TOP_K") is None
    # The unknown built-in was skipped with a warning; the next entry applied.
    assert any("from_a_newer_build" in w for w in res["warnings"])
    assert cfg("browser", "variable", "BROWSER_HOST") == "example.com"
    assert "configured browser" in res["applied"]
    # Nothing was written for src (profiles stay apart).
    assert ts.get_config(profile="src", tool_id="documentation_search",
                         scope="variable", key="RESEARCH_MODEL_GROUP") is None
