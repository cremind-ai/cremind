"""Working directories through setup and the server config.

Before per-profile folders the setup wizard wrote ONE ``server_config``
``user_working_dir`` every profile shared. Now:

- first setup takes the admin's folder from a top-level ``working_dir`` — or,
  from an older CLI or wizard, ``server_config.user_working_dir`` — validates
  and creates it, stores it on the admin's profile row (the default as NULL),
  and writes no server-wide key;
- an additional profile gets its own default folder (or the one the admin
  picked for it), created at setup;
- ``PUT /api/config/server`` refuses ``user_working_dir`` outright, and
  ``GET`` never shows a stale row;
- ``GET /api/config/setup-profiles`` suggests the folder the profile being set
  up will get, and tells it only to a caller entitled to see it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api
from app.config import working_dirs as wd

cfg = importlib.import_module("app.config.settings")


class _Store:
    """``working_dirs``' three methods, plus the ``get`` every
    ``get_dynamic`` read on the setup path makes (nothing is configured)."""

    def __init__(self, rows: dict[str, str | None]):
        self.rows = dict(rows)

    def get(self, *_a, **_kw):
        return None

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        if profile not in self.rows:
            return False
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


class _ConfigStorage:
    def __init__(self, *, complete: bool, server: dict[str, str] | None = None) -> None:
        self.complete = complete
        self.writes: list[tuple[str, str, str]] = []
        self.server = dict(server or {})

    def is_setup_complete(self) -> bool:
        return self.complete

    def mark_setup_complete(self) -> None:
        self.complete = True

    def get(self, table: str, key: str, **_kw: Any) -> str | None:
        if (table, key) == ("server_config", "jwt_secret"):
            return "test-jwt-secret-long-enough-for-hs256"
        return None

    def get_all(self, table: str, include_secrets: bool = False) -> dict[str, str]:
        return dict(self.server) if table == "server_config" else {}

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str | None = None) -> None:
        self.writes.append((table, key, value))
        if table == "server_config":
            self.server[key] = value

    def server_keys(self) -> set[str]:
        return {k for t, k, _v in self.writes if t == "server_config"}


class _Conversations:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def profile_exists(self, name: str) -> bool:
        return name in self.store.rows

    async def create_profile(self, name: str) -> None:
        self.store.rows[name] = None


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    # A locked database choice: first setup skips provisioning a backend.
    (sysdir / "bootstrap.toml").write_text('db_provider = "sqlite"\n', encoding="utf-8")
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"__server__": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()

    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)
    monkeypatch.setattr(config_api, "_features_required_by_setup_payload", lambda _b: [])
    monkeypatch.setattr(config_api, "ensure_persona_file", lambda _p: None)
    monkeypatch.setattr(config_api, "_generate_token", lambda _s, _p, hours=None: ("t", "later"))

    async def _no_vectorstore(_config: dict) -> dict:
        return {}

    monkeypatch.setattr(config_api, "_resolve_vectorstore", _no_vectorstore)
    monkeypatch.setattr("app.lib.embedding_lifecycle.persist_embedding_config", lambda *_a: None)
    monkeypatch.setattr("app.auth.write_token_file", lambda p, _t: str(tmp_path / f"{p}.token"))
    yield SimpleNamespace(sysdir=sysdir, store=store)
    wd.invalidate()
    config_api._setup_in_flight.clear()


def _routes(env, config_storage) -> dict[tuple[str, str], Callable]:
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=config_storage,
        conversation_storage=_Conversations(env.store),
        registry=None,
        on_first_setup=None,
        boot_fn=None,
    )
    out: dict[tuple[str, str], Callable] = {}
    for route in config_api.get_config_routes(state):  # type: ignore[arg-type]
        for method in route.methods or ():
            out[(route.path, method)] = route.endpoint
    return out


def _request(body: Any = None, *, user: str | None = None, query: dict | None = None):
    async def _json():
        return body

    return SimpleNamespace(
        headers={"host": "localhost:1515"},
        cookies={},
        json=_json,
        query_params=query or {},
        user=SimpleNamespace(is_authenticated=user is not None, username=user or ""),
    )


def _setup(env, body: dict, *, complete: bool = False):
    storage = _ConfigStorage(complete=complete)
    resp = asyncio.run(_routes(env, storage)[("/api/config/setup", "POST")](_request(body)))
    return resp.status_code, json.loads(resp.body), storage


# ── first setup ──────────────────────────────────────────────────────────────


def test_first_setup_stores_the_admins_folder_on_its_profile(env, tmp_path: Path) -> None:
    folder = tmp_path / "work"
    status, body, storage = _setup(env, {"profile": "admin", "working_dir": str(folder)})
    assert status == 200, body
    assert env.store.rows["admin"] == os.path.normpath(str(folder))
    assert folder.is_dir(), "setup creates the folder"
    assert Path(body["working_dir"]) == folder
    assert "user_working_dir" not in storage.server_keys(), "no server-wide folder any more"


def test_first_setup_without_one_uses_the_default(env) -> None:
    status, body, storage = _setup(env, {"profile": "admin"})
    assert status == 200, body
    default = env.sysdir / "workspaces" / "admin"
    assert env.store.rows["admin"] is None, "the default is stored as NULL"
    assert default.is_dir()
    assert Path(body["working_dir"]) == default
    assert "user_working_dir" not in storage.server_keys()


def test_an_older_clients_server_config_key_is_still_the_admins_folder(env, tmp_path: Path) -> None:
    folder = tmp_path / "legacy"
    status, body, storage = _setup(
        env, {"profile": "admin", "server_config": {"user_working_dir": str(folder), "log_level": "info"}},
    )
    assert status == 200, body
    assert env.store.rows["admin"] == os.path.normpath(str(folder))
    assert folder.is_dir()
    assert storage.server_keys() >= {"log_level"}
    assert "user_working_dir" not in storage.server_keys()


def test_a_reconfigure_rerun_without_one_keeps_the_admins_folder(env, tmp_path: Path) -> None:
    """Re-running first setup must not silently move the admin's files away
    (an upgraded install's admin kept its old server-wide folder)."""
    env.store.rows.update({"admin": str(tmp_path / "Documents"), "javis": None})
    status, body, _ = _setup(env, {"profile": "admin"})
    assert status == 200, body
    assert env.store.rows["admin"] == str(tmp_path / "Documents")
    assert Path(body["working_dir"]) == tmp_path / "Documents"


def test_a_rerun_may_still_reset_it_explicitly(env, tmp_path: Path) -> None:
    env.store.rows["admin"] = str(tmp_path / "Documents")
    status, body, _ = _setup(env, {"profile": "admin", "working_dir": ""})
    assert status == 200, body
    assert env.store.rows["admin"] is None


def test_the_suggested_default_follows_a_relocated_system_dir(env, tmp_path: Path, monkeypatch) -> None:
    """The wizard suggested ``<old SYS>/workspaces/admin`` and sends it back
    unchanged, while the admin also moved the System Directory. That is the
    default — under the NEW System Directory — not an explicit folder left
    behind under the one just abandoned."""
    old_default = env.sysdir / "workspaces" / "admin"
    new_sys = tmp_path / "moved-sys"

    def _relocate(path: str) -> str:
        new_sys.mkdir()
        shutil.copy(env.sysdir / "bootstrap.toml", new_sys / "bootstrap.toml")
        monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(new_sys))
        return str(new_sys)

    monkeypatch.setattr(config_api, "relocate_system_directory", _relocate)
    status, body, _ = _setup(env, {
        "profile": "admin", "working_dir": str(old_default),
        "server_config": {"system_dir": str(new_sys)},
    })
    assert status == 200, body
    assert env.store.rows["admin"] is None, "the default, stored as NULL"
    assert Path(body["working_dir"]) == new_sys / "workspaces" / "admin"
    assert (new_sys / "workspaces" / "admin").is_dir()
    assert not old_default.exists(), "nothing made under the abandoned System Directory"
    # The second profile lands beside it.
    status, body, _ = _setup(env, {"profile": "javis"}, complete=True)
    assert status == 200 and Path(body["working_dir"]) == new_sys / "workspaces" / "javis", body


def test_a_chosen_folder_survives_a_relocation(env, tmp_path: Path, monkeypatch) -> None:
    new_sys = tmp_path / "moved-sys"

    def _relocate(path: str) -> str:
        new_sys.mkdir()
        shutil.copy(env.sysdir / "bootstrap.toml", new_sys / "bootstrap.toml")
        monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(new_sys))
        return str(new_sys)

    monkeypatch.setattr(config_api, "relocate_system_directory", _relocate)
    folder = tmp_path / "work"
    status, body, _ = _setup(env, {
        "profile": "admin", "working_dir": str(folder), "server_config": {"system_dir": str(new_sys)},
    })
    assert status == 200, body
    assert env.store.rows["admin"] == os.path.normpath(str(folder))


@pytest.mark.parametrize("name", ["__ops", "__server__"])
def test_setup_refuses_a_pseudo_profile_name(env, name: str) -> None:
    """``__…`` is Cremind's own (``valid_profile_dirname`` gives such a name no
    working directory): refused by setup and, a round trip earlier, by the
    CLI wizard's draft."""
    env.store.rows["admin"] = None
    status, body, _ = _setup(env, {"profile": name}, complete=True)
    assert status == 400 and "'__'" in body["error"], body
    assert name not in env.store.rows or name == "__server__"
    from app.cli import wizard_draft

    with pytest.raises(ValueError, match="'__'"):
        wizard_draft.validate_profile_name(name)
    assert wizard_draft.validate_profile_name("ops_team") == "ops_team"


def test_a_bad_folder_fails_first_setup_before_anything_is_created(env) -> None:
    status, body, storage = _setup(env, {"profile": "admin", "working_dir": str(env.sysdir / "storage")})
    assert status == 400, body
    assert body["code"] == "invalid_working_dir" and body["reason"] == "inside_system_dir"
    assert "Working directory" in body["error"]
    assert "admin" not in env.store.rows
    assert storage.writes == []


# ── additional profiles ──────────────────────────────────────────────────────


def test_an_additional_profile_gets_its_own_default_folder(env) -> None:
    env.store.rows["admin"] = None
    status, body, _ = _setup(env, {"profile": "javis"}, complete=True)
    assert status == 200, body
    folder = env.sysdir / "workspaces" / "javis"
    assert folder.is_dir()
    assert Path(body["working_dir"]) == folder
    assert env.store.rows["javis"] is None


def test_an_additional_profile_never_inherits_a_folder_with_files(env) -> None:
    env.store.rows["admin"] = None
    old = env.sysdir / "workspaces" / "javis"
    old.mkdir(parents=True)
    (old / "left.txt").write_text("x")
    status, body, _ = _setup(env, {"profile": "javis"}, complete=True)
    assert status == 200, body
    assert Path(body["working_dir"]) == env.sysdir / "workspaces" / "javis-2"
    assert env.store.rows["javis"] == str(env.sysdir / "workspaces" / "javis-2")


def test_the_admin_may_pick_an_additional_profiles_folder(env, tmp_path: Path) -> None:
    env.store.rows["admin"] = None
    folder = tmp_path / "javis-files"
    status, body, _ = _setup(env, {"profile": "javis", "working_dir": str(folder)}, complete=True)
    assert status == 200, body
    assert env.store.rows["javis"] == os.path.normpath(str(folder))
    assert folder.is_dir()


def test_a_bad_folder_for_an_additional_profile_is_a_400_and_no_profile(env) -> None:
    env.store.rows["admin"] = None
    status, body, _ = _setup(env, {"profile": "javis", "working_dir": "not/absolute"}, complete=True)
    assert status == 400 and body["reason"] == "not_absolute", body
    assert "javis" not in env.store.rows


def test_adopting_keeps_the_existing_folder_and_says_so(env, tmp_path: Path) -> None:
    env.store.rows.update({"admin": None, "javis": str(tmp_path / "kept")})
    status, body, _ = _setup(
        env,
        {"profile": "javis", "adopt_existing": True, "working_dir": str(tmp_path / "other")},
        complete=True,
    )
    assert status == 200, body
    assert env.store.rows["javis"] == str(tmp_path / "kept")
    assert any(w["code"] == "working_dir_ignored" for w in body["warnings"])


# ── server config ────────────────────────────────────────────────────────────


def test_server_config_refuses_the_old_key_and_writes_nothing(env) -> None:
    storage = _ConfigStorage(complete=True)
    routes = _routes(env, storage)
    resp = asyncio.run(routes[("/api/config/server", "PUT")](
        _request({"config": {"log_level": "debug", "user_working_dir": "/srv/x"}}, user="admin"),
    ))
    body = json.loads(resp.body)
    assert resp.status_code == 400, body
    assert "user_working_dir" in body["error"]
    assert "cremind profile working-dir" in body["message"]
    assert storage.writes == [], "a mixed request applies none of it"


def test_server_config_never_shows_a_stale_row(env) -> None:
    storage = _ConfigStorage(complete=True, server={"user_working_dir": "/old", "log_level": "info"})
    routes = _routes(env, storage)
    resp = asyncio.run(routes[("/api/config/server", "GET")](_request(user="admin")))
    config = json.loads(resp.body)["config"]
    assert config == {"log_level": "info"}


# ── the wizard's suggestion ──────────────────────────────────────────────────


def _suggest(env, *, complete: bool, user: str | None = None, query: dict | None = None) -> dict:
    routes = _routes(env, _ConfigStorage(complete=complete))
    resp = asyncio.run(routes[("/api/config/setup-profiles", "GET")](_request(user=user, query=query)))
    return json.loads(resp.body)


def test_first_setup_suggests_the_admins_default_and_lets_it_be_edited(env) -> None:
    body = _suggest(env, complete=False)
    assert Path(body["suggested_working_dir"]) == env.sysdir / "workspaces" / "admin"
    assert body["working_dir_editable"] is True


def test_a_rerun_suggests_the_admins_current_folder(env, tmp_path: Path) -> None:
    env.store.rows["admin"] = str(tmp_path / "mine")
    body = _suggest(env, complete=False)
    assert Path(body["suggested_working_dir"]) == tmp_path / "mine"


def test_after_setup_admin_sees_the_new_profiles_folder_read_only(env) -> None:
    env.store.rows["admin"] = None
    body = _suggest(env, complete=True, user="admin", query={"profile": "bob"})
    assert Path(body["suggested_working_dir"]) == env.sysdir / "workspaces" / "bob"
    assert body["working_dir_editable"] is False


def test_after_setup_an_anonymous_or_other_caller_is_told_nothing(env) -> None:
    env.store.rows.update({"admin": None, "javis": None})
    assert _suggest(env, complete=True)["suggested_working_dir"] is None
    assert _suggest(env, complete=True, user="javis", query={"profile": "admin"})["suggested_working_dir"] is None
    own = _suggest(env, complete=True, user="javis")
    assert Path(own["suggested_working_dir"]) == env.sysdir / "workspaces" / "javis"
