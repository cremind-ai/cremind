"""API: Documentation search settings and the admin gate.

What these pin, in order of how much damage a regression would do:

- **Profile isolation.** Every route acts on the caller's own profile. There is
  no body or query field that names a profile, so there is nothing to forge.
- **The gate order.** A profile cannot turn the feature on until the admin has
  allowed it and Vector Embedding is on — and the admin cannot allow it while
  the extraction extras are missing (same 409 shape as the embedding PUT, so
  the existing install dialog and CLI hint work unchanged).
- **Confirm before destroy.** A change that would remove indexed content is
  refused with a plan and a token; only the same change carrying that token is
  applied.
- **The folder is the working directory.** Each profile indexes its own
  working directory; a settings save cannot name another folder, and a working
  directory that is unsafe to index is refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402
from starlette.datastructures import QueryParams  # noqa: E402

import app.storage as storage_pkg  # noqa: E402
import app.storage.documents_storage as uds_storage_module  # noqa: E402
from app.api import documents as api  # noqa: E402
from app.config.settings import BaseConfig  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.documents_storage import DocumentsStorage  # noqa: E402
from app.documents import settings as uds  # noqa: E402
from app.documents import state as uds_state  # noqa: E402
from tests.documents._workspaces import install  # noqa: E402

_TABLES = ("profiles", "document_sources", "document_captions", "document_vision_usage")


class _Req:
    def __init__(self, username="admin", body=None, query="", authenticated=True):
        self.user = SimpleNamespace(is_authenticated=authenticated, username=username)
        self.query_params = QueryParams(query)
        self._body = body or {}

    async def json(self):
        return self._body


class _FakeConfig:
    def __init__(self, rows):
        self.rows = rows

    def set(self, table, key, value, **_kw):
        self.rows[key] = value


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "ud.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    now = time.time() * 1000
    with eng.begin() as c:
        for i, name in enumerate(("admin", "dog")):
            c.execute(text(
                "INSERT INTO profiles (id,name,created_at,updated_at) "
                f"VALUES ('p{i}','{name}',:n,:n)"
            ), {"n": now})

    storage = DocumentsStorage(provider)
    monkeypatch.setattr(uds_storage_module, "_instance", storage)

    sysdir = tmp_path / "system"
    outside = tmp_path / "elsewhere"
    for d in (sysdir, outside):
        d.mkdir(parents=True, exist_ok=True)
    # Both profiles on their default working directories, <SYS>/workspaces/<name>.
    working_dirs = install(monkeypatch, sysdir, {"admin": None, "dog": None})

    rows: dict[str, str] = {}
    monkeypatch.setattr(uds, "get_dynamic", lambda table, key, *a, **k: rows.get(key))
    monkeypatch.setattr(storage_pkg, "get_dynamic_config_storage", lambda *a, **k: _FakeConfig(rows))
    embedding = {"on": True}
    monkeypatch.setattr(
        BaseConfig, "is_embedding_enabled", classmethod(lambda cls: embedding["on"]),
    )
    monkeypatch.setattr(api, "_feature_missing", lambda: [])
    monkeypatch.setattr(api, "_drive_view", lambda profile: {"linked": False})

    purged: list[tuple[str, str]] = []
    uds_state.set_purge_handler(lambda p, k: purged.append((p, k)))
    yield SimpleNamespace(
        storage=storage, rows=rows, sysdir=sysdir, outside=outside, working_dirs=working_dirs,
        wd={p: sysdir / "workspaces" / p for p in ("admin", "dog")},
        embedding=embedding, purged=purged, monkeypatch=monkeypatch,
    )
    uds_state.set_purge_handler(None)


def _route(path: str, method: str):
    for r in api.get_documents_routes():
        if r.path == path and method in (r.methods or ()):
            return r.endpoint
    raise AssertionError(f"no route {method} {path}")


def _call(path, method, **req):
    return asyncio.run(_route(path, method)(_Req(**req)))


def _allow(env):
    env.rows["documentation_search.allowed"] = "true"


# ── auth ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,method", [
    ("/api/documentation-search/status", "GET"),
    ("/api/documentation-search/settings", "GET"),
    ("/api/documentation-search/settings", "PUT"),
    ("/api/documentation-search/admin", "GET"),
])
def test_everything_needs_a_token(env, path, method):
    assert _call(path, method, authenticated=False).status_code == 401


def test_there_is_no_folder_picker_any_more():
    """The folder is the working directory: no browse, no validate-root."""
    paths = {r.path for r in api.get_documents_routes()}
    assert "/api/documentation-search/browse" not in paths
    assert "/api/documentation-search/validate-root" not in paths


def test_the_gate_is_admin_only(env):
    assert _call("/api/documentation-search/admin", "GET", username="dog").status_code == 403
    resp = _call("/api/documentation-search/admin", "PUT", username="dog", body={"allowed": True})
    assert resp.status_code == 403
    assert "documentation_search.allowed" not in env.rows


# ── admin gate ─────────────────────────────────────────────────────────────


def test_allowing_needs_vector_embedding(env):
    env.embedding["on"] = False
    resp = _call("/api/documentation-search/admin", "PUT", body={"allowed": True})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "EmbeddingDisabled"


def test_allowing_needs_the_extras_in_the_embedding_put_shape(env):
    missing = [{"feature_key": "documentation_search", "extras": ["documents", "documentation-search"],
                "requires_restart_after_install": False}]
    env.monkeypatch.setattr(api, "_feature_missing", lambda: missing)
    resp = _call("/api/documentation-search/admin", "PUT", body={"allowed": True})
    assert resp.status_code == 409
    body = _body(resp)
    assert body["error"] == "FeatureNotInstalled"
    assert body["missing"] == missing


def test_admin_can_allow_and_set_budgets(env):
    resp = _call("/api/documentation-search/admin", "PUT", body={"allowed": True, "storage_budget_mb": 4096})
    assert resp.status_code == 200
    assert _body(resp)["policy"]["allowed"] is True
    assert env.rows["documentation_search.storage_budget_mb"] == "4096"

    bad = _call("/api/documentation-search/admin", "PUT", body={"workers": 0})
    assert bad.status_code == 400
    assert "workers" in _body(bad)["details"]


def test_the_admin_overview_never_shows_other_profiles_paths(env):
    _allow(env)
    env.storage.upsert_source("dog", "local", enabled=True, root_path=str(env.wd["dog"]))
    body = _body(_call("/api/documentation-search/admin", "GET"))
    assert body["profiles"] == [{"profile": "dog", "local_enabled": True}]
    assert "workspaces" not in json.dumps(body["profiles"])


# ── per-profile settings ───────────────────────────────────────────────────


def test_a_profile_cannot_enable_before_the_admin_allows_it(env):
    resp = _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "FeatureDisabledByAdmin"
    assert env.storage.get_source("dog", "local") is None


def test_enable_stores_the_profiles_own_working_directory(env):
    """Each profile's own — the default one lives inside the system folder,
    and that is exactly the one folder there that may be indexed."""
    _allow(env)
    for profile in ("dog", "admin"):
        resp = _call("/api/documentation-search/settings", "PUT", username=profile, body={"enabled": True})
        assert resp.status_code == 200, resp.body
        row = env.storage.get_source(profile, "local")
        assert row["enabled"] is True
        assert row["root_mode"] == "inherit"
        # Stored, so a later change of the working directory is noticed
        # instead of silently re-pointing the index.
        assert row["root_path"] == os.path.realpath(env.wd[profile])


def test_the_settings_show_the_callers_own_working_directory(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    mine = _body(_call("/api/documentation-search/settings", "GET", username="admin"))
    assert mine["local"]["enabled"] is False
    assert mine["local"]["root_path"] is None
    assert mine["policy_view"]["working_dir"] == os.path.realpath(env.wd["admin"])
    assert mine["policy_view"]["working_dir_editable"] is True
    assert "root_constraint" not in mine["policy_view"]
    assert "root_mode" not in mine["local"]
    dogs = _body(_call("/api/documentation-search/settings", "GET", username="dog"))
    assert dogs["policy_view"]["working_dir"] == os.path.realpath(env.wd["dog"])
    assert dogs["policy_view"]["working_dir_editable"] is False
    status = _body(_call("/api/documentation-search/status", "GET", username="admin"))
    assert status["enabled"] is False


@pytest.mark.parametrize("field,value", [
    ("root_path", "elsewhere"),
    ("root_mode", "custom"),
    ("root_mode", "inherit"),
])
def test_a_settings_save_cannot_choose_a_folder(env, field, value):
    _allow(env)
    body = {"enabled": True, field: str(env.outside) if value == "elsewhere" else value}
    for profile in ("dog", "admin"):
        resp = _call("/api/documentation-search/settings", "PUT", username=profile, body=body)
        assert resp.status_code == 400
        out = _body(resp)
        assert out["code"] == "root_not_configurable"
        assert "working directory" in out["message"]
        assert env.storage.get_source(profile, "local") is None


def test_a_working_directory_unsafe_to_index_is_refused(env):
    """The admin pointed dog's working directory into the system folder (not
    its own default workspace there): enabling says why and stores nothing."""
    _allow(env)
    (env.sysdir / "storage").mkdir()
    env.working_dirs.rows["dog"] = str(env.sysdir / "storage")
    resp = _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    assert resp.status_code == 400
    assert _body(resp)["code"] == "inside_system_dir"
    assert env.storage.get_source("dog", "local") is None


def test_a_moved_working_directory_is_not_moved_by_a_settings_save(env):
    """Later saves never re-point the stored folder: the engine notices the
    move and asks the profile first (pending_root_change)."""
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    env.working_dirs.rows["dog"] = str(env.outside)
    resp = _call("/api/documentation-search/settings", "PUT", username="dog",
                 body={"options": {"caption": {"daily_cap": 5}}})
    assert resp.status_code == 200, resp.body
    row = env.storage.get_source("dog", "local")
    assert row["root_path"] == os.path.realpath(env.wd["dog"])
    view = _body(resp)["settings"]
    assert view["policy_view"]["working_dir"] == os.path.realpath(env.outside)
    assert view["local"]["root_path"] == os.path.realpath(env.wd["dog"])


def test_new_excludes_need_the_matching_confirmation(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    change = {"excludes": ["Archive/**"]}

    first = _call("/api/documentation-search/settings", "PUT", username="dog", body=change)
    assert first.status_code == 409
    body = _body(first)
    assert body["error"] == "ConfirmationRequired"
    assert body["plan"]["destructive"] is True
    token = body["confirm"]

    # The token confirms exactly that change — not another one.
    other = _call("/api/documentation-search/settings", "PUT", username="dog",
                  body={"excludes": ["Other/**"], "confirm": token})
    assert other.status_code == 409
    assert _body(other)["confirm"] != token
    assert env.storage.get_source("dog", "local")["excludes"] in (None, [])

    applied = _call("/api/documentation-search/settings", "PUT", username="dog", body={**change, "confirm": token})
    assert applied.status_code == 200, applied.body
    assert env.storage.get_source("dog", "local")["excludes"] == [
        {"pattern": "Archive/**", "type": "glob", "mode": "skip"},
    ]


def test_disable_keeps_the_index_unless_asked(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})

    keep = _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": False})
    assert keep.status_code == 200
    assert env.purged == []

    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    ask = _call("/api/documentation-search/settings", "PUT", username="dog",
                body={"enabled": False, "delete_index": True})
    assert ask.status_code == 409
    token = _body(ask)["confirm"]
    done = _call("/api/documentation-search/settings", "PUT", username="dog",
                 body={"enabled": False, "delete_index": True, "confirm": token})
    assert done.status_code == 200, done.body
    assert env.purged == [("dog", "local")]


def test_delete_index_is_refused_while_staying_enabled(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    resp = _call("/api/documentation-search/settings", "PUT", username="dog", body={"delete_index": True})
    assert resp.status_code == 400


def test_drive_needs_a_linked_account(env):
    _allow(env)
    env.monkeypatch.setattr(api, "_drive_linked", lambda profile: False)
    resp = _call("/api/documentation-search/settings", "PUT", username="dog",
                 body={"kind": "drive", "enabled": True})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "DriveNotLinked"


def test_status_reports_the_suspension_reason(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    env.embedding["on"] = False
    snap = _body(_call("/api/documentation-search/status", "GET", username="dog"))
    assert (snap["state"], snap["reason"], snap["tool_mode"]) == (
        "suspended", "embedding_off", "lexical_only",
    )
    env.rows["documentation_search.allowed"] = "false"
    snap = _body(_call("/api/documentation-search/status", "GET", username="dog"))
    assert (snap["state"], snap["reason"], snap["tool_mode"]) == ("suspended", "admin_gate", "hidden")


def test_status_shows_a_profile_its_own_relocation_problems_by_uuid(env):
    """An index step knows only the uuid, so a filter by name never showed a
    profile its own index conflict; and a name can be reused, so a step that
    names ``dog`` but belongs to an earlier profile of that name must stay
    hidden from the current one. The admin sees them all."""
    from app.documents import relocate

    journal = relocate.Journal.load(env.sysdir)
    journal.record("index:p1", state="conflict", kind="index", uid="p1", error="dog's index")
    journal.record("index:p0", state="conflict", kind="index", uid="p0", profile="admin", error="admin's index")
    journal.record("authored:p9", state="conflict", kind="authored", uid="p9", profile="dog",
                   error="an earlier profile named dog")
    journal.record("indexes", state="error", error="installation-wide")

    dog = _body(_call("/api/documentation-search/status", "GET", username="dog"))["relocation_errors"]
    assert [p["step"] for p in dog] == ["index:p1"]
    admin = _body(_call("/api/documentation-search/status", "GET", username="admin"))["relocation_errors"]
    assert {p["step"] for p in admin} == {"index:p1", "index:p0", "authored:p9", "indexes"}
