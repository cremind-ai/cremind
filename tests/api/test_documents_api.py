"""API: Documentation search settings, the admin gate, and folder browsing.

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
- **The root boundary.** A non-admin profile cannot index outside the working
  directory, and nobody can browse into Cremind's system folder.
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

    wd = tmp_path / "work"
    sysdir = tmp_path / "system"
    outside = tmp_path / "elsewhere"
    for d in (wd / "Reports", wd / "Other", wd / "coding-cli", wd / ".hidden", sysdir, outside):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setattr(uds, "get_user_working_directory", lambda: str(wd))

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
        storage=storage, rows=rows, wd=wd, sysdir=sysdir, outside=outside,
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
    ("/api/documentation-search/validate-root", "POST"),
    ("/api/documentation-search/browse", "GET"),
    ("/api/documentation-search/admin", "GET"),
])
def test_everything_needs_a_token(env, path, method):
    assert _call(path, method, authenticated=False).status_code == 401


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
    env.storage.upsert_source("dog", "local", enabled=True, root_path=str(env.wd / "Reports"))
    body = _body(_call("/api/documentation-search/admin", "GET"))
    assert body["profiles"] == [{"profile": "dog", "local_enabled": True}]
    assert str(env.wd) not in json.dumps(body["profiles"])


# ── per-profile settings ───────────────────────────────────────────────────


def test_a_profile_cannot_enable_before_the_admin_allows_it(env):
    resp = _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    assert resp.status_code == 409
    assert _body(resp)["error"] == "FeatureDisabledByAdmin"
    assert env.storage.get_source("dog", "local") is None


def test_enable_stores_the_resolved_working_directory(env):
    _allow(env)
    resp = _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    assert resp.status_code == 200, resp.body
    row = env.storage.get_source("dog", "local")
    assert row["enabled"] is True
    assert row["root_mode"] == "inherit"
    # Stored even for "inherit", so a later change of the global working
    # directory is noticed instead of silently re-pointing the index.
    assert row["root_path"] == os.path.realpath(env.wd)


def test_profiles_only_ever_see_their_own_settings(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog",
          body={"enabled": True, "root_path": str(env.wd / "Reports")})
    mine = _body(_call("/api/documentation-search/settings", "GET", username="admin"))
    assert mine["local"]["enabled"] is False
    assert mine["local"]["root_path"] is None
    status = _body(_call("/api/documentation-search/status", "GET", username="admin"))
    assert status["enabled"] is False


def test_a_member_cannot_index_outside_the_working_directory(env):
    _allow(env)
    resp = _call("/api/documentation-search/settings", "PUT", username="dog",
                 body={"enabled": True, "root_path": str(env.outside)})
    assert resp.status_code == 400
    assert _body(resp)["code"] == "outside_working_dir"
    assert env.storage.get_source("dog", "local") is None


def test_nobody_can_index_the_system_folder(env):
    _allow(env)
    resp = _call("/api/documentation-search/settings", "PUT", username="admin",
                 body={"enabled": True, "root_path": str(env.sysdir)})
    assert resp.status_code == 400
    assert _body(resp)["code"] == "inside_system_dir"


def test_moving_the_folder_needs_the_matching_confirmation(env):
    _allow(env)
    _call("/api/documentation-search/settings", "PUT", username="dog", body={"enabled": True})
    move = {"root_path": str(env.wd / "Reports")}

    first = _call("/api/documentation-search/settings", "PUT", username="dog", body=move)
    assert first.status_code == 409
    body = _body(first)
    assert body["error"] == "ConfirmationRequired"
    assert body["plan"]["destructive"] is True
    token = body["confirm"]

    # The token confirms exactly that move — not a move somewhere else.
    elsewhere = {"root_path": str(env.wd / "Other"), "confirm": token}
    other = _call("/api/documentation-search/settings", "PUT", username="dog", body=elsewhere)
    assert other.status_code == 409
    assert _body(other)["confirm"] != token
    assert env.storage.get_source("dog", "local")["root_path"] == os.path.realpath(env.wd)

    applied = _call("/api/documentation-search/settings", "PUT", username="dog", body={**move, "confirm": token})
    assert applied.status_code == 200, applied.body
    assert env.storage.get_source("dog", "local")["root_path"] == os.path.realpath(env.wd / "Reports")


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


# ── browse ─────────────────────────────────────────────────────────────────


def test_browse_hides_credentials_hidden_folders_and_the_system_folder(env):
    body = _body(_call("/api/documentation-search/browse", "GET", username="dog"))
    names = {e["name"] for e in body["entries"]}
    assert "Reports" in names
    assert "coding-cli" not in names
    assert ".hidden" not in names


def test_a_member_cannot_browse_outside_the_working_directory(env):
    resp = _call("/api/documentation-search/browse", "GET", username="dog",
                 query=f"path={env.outside}")
    assert resp.status_code == 403
    assert _call("/api/documentation-search/browse", "GET", username="admin",
                 query=f"path={env.outside}").status_code == 200
    assert _call("/api/documentation-search/browse", "GET", username="admin",
                 query=f"path={env.sysdir}").status_code == 403
