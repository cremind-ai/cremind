"""Each profile's working directory through ``/api/profiles``.

The folder is where a profile's files live and the fence that keeps the other
profiles out of them, so who may move it matters as much as where it goes:

- ``GET /api/profiles/{name}/working-dir`` — the owner, or admin; anyone else 403.
- ``PUT`` — **admin only**, for every profile including a non-admin's own. The
  server validates the folder (``validate_working_dir``) and answers a refusal
  with ``400 {error: "InvalidWorkingDir", code, message}``; the default path
  (or ``null``) is stored as NULL.
- ``POST /api/profiles`` gives a new profile its folder — the admin's choice, or
  its default, or a fresh ``<name>-2`` when a folder of that name already holds
  files: a new profile never inherits someone else's files.
- ``DELETE`` keeps a Cremind-made folder by default (moved to
  ``<workspaces>/.deleted``), removes it on ``working_dir=delete``, and never
  touches a folder the admin chose elsewhere.

Two profiles throughout (``admin`` + ``javis``), per CLAUDE.md's profile check.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api.profiles import get_profile_routes
from app.config import working_dirs as wd

cfg = importlib.import_module("app.config.settings")


class _Store:
    """The three DynamicConfigStorage methods ``working_dirs`` uses."""

    def __init__(self, rows: dict[str, str | None]):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        if profile not in self.rows:
            return False
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


class _Conversations:
    """Profile rows backed by the same store, so ownership sees creates/deletes."""

    def __init__(self, store: _Store):
        self.store = store

    async def list_profiles(self):
        return [{"name": n} for n in self.store.rows]

    async def profile_exists(self, name: str) -> bool:
        return name in self.store.rows

    async def create_profile(self, name: str) -> dict:
        self.store.rows[name] = None
        return {"name": name}

    async def delete_profile(self, name: str) -> bool:
        return self.store.rows.pop(name, "missing") != "missing"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"admin": None, "javis": None, "__server__": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()

    from app.groups import boot as groups_boot

    async def _noop(_profile: str) -> None:
        return None

    monkeypatch.setattr(groups_boot, "on_profile_deleted", _noop)
    yield SimpleNamespace(sysdir=sysdir, store=store, conversations=_Conversations(store))
    wd.invalidate()


def _route(env, path: str, method: str) -> Callable:
    for route in get_profile_routes(env.conversations, registry=None):  # type: ignore[arg-type]
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _request(user: str | None, name: str | None = None, *, body: Any = None, query: dict | None = None):
    async def _json():
        if body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return body

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=user is not None, username=user or ""),
        path_params={"profile_name": name} if name is not None else {},
        query_params=query or {},
        json=_json,
    )


def _call(env, path: str, method: str, request) -> tuple[int, dict]:
    resp = asyncio.run(_route(env, path, method)(request))
    return resp.status_code, json.loads(resp.body)


_WD = "/api/profiles/{profile_name}/working-dir"
_ONE = "/api/profiles/{profile_name}"
_ALL = "/api/profiles"


def _default(env, name: str) -> Path:
    return env.sysdir / "workspaces" / name


# ── GET ──────────────────────────────────────────────────────────────────────


def test_the_owner_reads_its_own_folder(env) -> None:
    status, body = _call(env, _WD, "GET", _request("javis", "javis"))
    assert status == 200, body
    assert Path(body["path"]) == _default(env, "javis")
    assert Path(body["default_path"]) == _default(env, "javis")
    assert body["is_default"] is True
    assert body["profile"] == "javis"
    assert body["exists"] is False, "reading creates nothing"


def test_admin_reads_any_profiles_folder(env, tmp_path: Path) -> None:
    env.store.rows["javis"] = str(tmp_path / "team")
    status, body = _call(env, _WD, "GET", _request("admin", "javis"))
    assert status == 200, body
    assert Path(body["path"]) == tmp_path / "team"
    assert body["is_default"] is False


def test_another_profile_may_not_read_it(env) -> None:
    status, body = _call(env, _WD, "GET", _request("javis", "admin"))
    assert status == 403, body


def test_an_unknown_profile_is_404(env) -> None:
    status, _ = _call(env, _WD, "GET", _request("admin", "ghost"))
    assert status == 404


def test_reading_requires_auth(env) -> None:
    status, _ = _call(env, _WD, "GET", _request(None, "javis"))
    assert status == 401


# ── PUT ──────────────────────────────────────────────────────────────────────


def test_a_non_admin_may_not_move_even_its_own_folder(env, tmp_path: Path) -> None:
    status, body = _call(env, _WD, "PUT", _request("javis", "javis", body={"path": str(tmp_path / "w")}))
    assert status == 403, body
    assert "admin" in body["error"]
    assert env.store.rows["javis"] is None
    assert not (tmp_path / "w").exists()


def test_admin_moves_a_profiles_folder_and_it_is_created(env, tmp_path: Path) -> None:
    target = tmp_path / "work" / "javis"
    status, body = _call(env, _WD, "PUT", _request("admin", "javis", body={"path": str(target)}))
    assert status == 200, body
    assert Path(body["path"]) == target and body["is_default"] is False and body["exists"] is True
    assert env.store.rows["javis"] == os.path.normpath(str(target))
    assert target.is_dir()
    assert env.store.rows["admin"] is None, "only the named profile changes"


@pytest.mark.parametrize("path_of", [lambda env: None, lambda env: str(_default(env, "javis"))])
def test_null_or_the_default_path_is_stored_as_null(env, tmp_path: Path, path_of) -> None:
    env.store.rows["javis"] = str(tmp_path / "old")
    status, body = _call(env, _WD, "PUT", _request("admin", "javis", body={"path": path_of(env)}))
    assert status == 200, body
    assert env.store.rows["javis"] is None
    assert body["is_default"] is True and body["exists"] is True


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("relative/folder", "not_absolute"),
        ("{sys}/storage", "inside_system_dir"),
    ],
)
def test_a_refused_folder_is_400_with_the_reason(env, raw: str, code: str) -> None:
    status, body = _call(
        env, _WD, "PUT", _request("admin", "javis", body={"path": raw.format(sys=env.sysdir)}),
    )
    assert status == 400, body
    assert body["error"] == "InvalidWorkingDir"
    assert body["code"] == code and body["message"]
    assert env.store.rows["javis"] is None


def test_a_folder_inside_the_workspaces_root_is_refused(env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(tmp_path / "mount"))
    wd.invalidate()
    status, body = _call(
        env, _WD, "PUT", _request("admin", "javis", body={"path": str(tmp_path / "mount" / "x")}),
    )
    assert status == 400 and body["code"] == "inside_workspaces", body


def test_carving_into_another_profiles_folder_is_refused_but_sharing_it_is_not(env, tmp_path: Path) -> None:
    team = tmp_path / "team"
    team.mkdir()
    env.store.rows["admin"] = str(team)
    wd.invalidate()
    status, body = _call(env, _WD, "PUT", _request("admin", "javis", body={"path": str(team / "sub")}))
    assert status == 400 and body["code"] == "inside_other_working_dir", body
    status, body = _call(env, _WD, "PUT", _request("admin", "javis", body={"path": str(team)}))
    assert status == 200, body


def test_put_needs_a_path_key(env) -> None:
    status, body = _call(env, _WD, "PUT", _request("admin", "javis", body={}))
    assert status == 400 and "path" in body["error"]


def test_put_on_an_unknown_profile_is_404(env, tmp_path: Path) -> None:
    status, _ = _call(env, _WD, "PUT", _request("admin", "ghost", body={"path": str(tmp_path / "g")}))
    assert status == 404


def test_a_change_notifies_the_listeners(env, tmp_path: Path) -> None:
    seen: list[str] = []
    wd.add_change_listener(seen.append)
    try:
        _call(env, _WD, "PUT", _request("admin", "javis", body={"path": str(tmp_path / "n")}))
    finally:
        wd.remove_change_listener(seen.append)
    assert seen == ["javis"]


# ── create ───────────────────────────────────────────────────────────────────


def test_a_new_profile_gets_its_default_folder(env) -> None:
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob"}))
    assert status == 201, body
    assert Path(body["working_dir"]["path"]) == _default(env, "bob")
    assert body["working_dir"]["is_default"] is True
    assert env.store.rows["bob"] is None
    assert _default(env, "bob").is_dir()


def test_a_new_profile_never_adopts_a_folder_that_holds_files(env) -> None:
    leftover = _default(env, "bob")
    leftover.mkdir(parents=True)
    (leftover / "old.txt").write_text("someone else's")
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob"}))
    assert status == 201, body
    fresh = env.sysdir / "workspaces" / "bob-2"
    assert Path(body["working_dir"]["path"]) == fresh
    assert env.store.rows["bob"] == str(fresh), "stored explicitly — the default is taken"
    assert fresh.is_dir() and not any(fresh.iterdir())
    assert (leftover / "old.txt").exists(), "the old files are left alone"


def test_an_empty_leftover_folder_is_simply_reused(env) -> None:
    _default(env, "bob").mkdir(parents=True)
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob"}))
    assert status == 201, body
    assert env.store.rows["bob"] is None


def test_admin_may_choose_the_new_profiles_folder(env, tmp_path: Path) -> None:
    target = tmp_path / "bobs"
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob", "working_dir": str(target)}))
    assert status == 201, body
    assert env.store.rows["bob"] == os.path.normpath(str(target))
    assert target.is_dir()


def test_a_refused_folder_creates_no_profile(env) -> None:
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob", "working_dir": "rel/dir"}))
    assert status == 400 and body["code"] == "not_absolute", body
    assert "bob" not in env.store.rows


def test_the_workspaces_name_is_reserved(env) -> None:
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "workspaces"}))
    assert status == 400 and "reserved" in body["error"], body
    assert "workspaces" not in env.store.rows


@pytest.mark.parametrize("name", ["__ops", "__server__"])
def test_a_pseudo_profile_name_is_refused(env, name: str) -> None:
    """``__…`` names are Cremind's own; such a profile would have no working
    directory, so every one of its turns would fail."""
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": name}))
    assert status == 400 and "'__'" in body["error"], body
    assert name not in env.store.rows or name == "__server__"
    assert not (env.sysdir / "workspaces" / name).exists()


def test_a_new_profile_is_never_handed_a_live_profiles_folder(env) -> None:
    """``dan``'s default holds leftovers; ``<ws>/dan-2`` is the default of a
    live profile ``dan-2`` — empty, but not ``dan``'s to take."""
    status, _ = _call(env, _ALL, "POST", _request("admin", body={"name": "dan-2"}))
    assert status == 201
    _seed(_default(env, "dan"))
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "dan"}))
    assert status == 201, body
    assert Path(body["working_dir"]["path"]) == env.sysdir / "workspaces" / "dan-3"
    assert wd.is_foreign(str(_default(env, "dan-2") / "x"), "dan")
    assert wd.is_foreign(str(env.sysdir / "workspaces" / "dan-3" / "x"), "dan-2")


def test_a_new_profile_whose_default_is_another_profiles_sibling_gets_its_own(env) -> None:
    """``bob`` was given ``<ws>/bob-2`` (its default held files); a profile
    created later AS ``bob-2`` must not share ``bob``'s folder."""
    _seed(_default(env, "bob"))
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob"}))
    assert status == 201 and env.store.rows["bob"] == str(env.sysdir / "workspaces" / "bob-2"), body
    status, body = _call(env, _ALL, "POST", _request("admin", body={"name": "bob-2"}))
    assert status == 201, body
    assert Path(body["working_dir"]["path"]) == env.sysdir / "workspaces" / "bob-2-2"
    assert wd.is_foreign(str(env.sysdir / "workspaces" / "bob-2" / "x"), "bob-2")
    assert not wd.is_foreign(str(env.sysdir / "workspaces" / "bob-2" / "x"), "bob")


def test_deleting_a_profile_never_hands_its_sibling_to_the_profile_named_like_it(env) -> None:
    """``bob`` lives in ``<ws>/bob-2`` and a live profile is named ``bob-2``:
    when ``bob`` goes, its folder is archived — not left for ``bob-2`` to
    reach by name."""
    _seed(_default(env, "bob"))
    _call(env, _ALL, "POST", _request("admin", body={"name": "bob"}))
    _call(env, _ALL, "POST", _request("admin", body={"name": "bob-2"}))
    sibling = _seed(env.sysdir / "workspaces" / "bob-2")
    status, body = _call(env, _ONE, "DELETE", _request("admin", "bob"))
    assert status == 200, body
    assert body["working_dir"]["action"] == "archived", body
    assert not sibling.exists()
    archived = Path(body["working_dir"]["archived_to"])
    assert (archived / "notes.txt").read_text() == "mine"
    assert wd.is_foreign(str(archived / "notes.txt"), "bob-2")
    assert Path(wd.profile_working_dir("bob-2", create=False)) == env.sysdir / "workspaces" / "bob-2-2"


# ── delete ───────────────────────────────────────────────────────────────────


def _seed(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("mine")
    return folder


def test_delete_keeps_the_folder_by_moving_it_aside(env) -> None:
    folder = _seed(_default(env, "javis"))
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis"))
    assert status == 200, body
    info = body["working_dir"]
    assert info["action"] == "archived"
    moved = Path(info["archived_to"])
    assert moved.parent == env.sysdir / "workspaces" / ".deleted"
    assert moved.name.startswith("javis-")
    assert (moved / "notes.txt").read_text() == "mine"
    assert not folder.exists(), "a same-name profile must start empty"


def test_delete_may_remove_the_folder_with_its_files(env) -> None:
    folder = _seed(_default(env, "javis"))
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis", query={"working_dir": "delete"}))
    assert status == 200, body
    assert body["working_dir"]["action"] == "deleted"
    assert not folder.exists()
    assert not (env.sysdir / "workspaces" / ".deleted").exists()


def test_the_choice_may_also_come_in_the_body(env) -> None:
    folder = _seed(_default(env, "javis"))
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis", body={"working_dir": "delete"}))
    assert status == 200 and body["working_dir"]["action"] == "deleted", body
    assert not folder.exists()


def test_a_folder_the_admin_chose_elsewhere_is_never_touched(env, tmp_path: Path) -> None:
    custom = _seed(tmp_path / "custom")
    env.store.rows["javis"] = str(custom)
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis", query={"working_dir": "delete"}))
    assert status == 200, body
    assert body["working_dir"]["action"] == "untouched"
    assert (custom / "notes.txt").exists()


def test_a_cremind_made_sibling_is_archived_too(env) -> None:
    sibling = _seed(env.sysdir / "workspaces" / "javis-2")
    env.store.rows["javis"] = str(sibling)
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis"))
    assert status == 200, body
    assert body["working_dir"]["action"] == "archived"
    assert not sibling.exists()
    assert (Path(body["working_dir"]["archived_to"]) / "notes.txt").exists()


def test_a_missing_or_empty_folder_reports_nothing_to_keep(env) -> None:
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis"))
    assert status == 200 and body["working_dir"]["action"] == "none", body


def test_an_unknown_choice_is_400_and_deletes_nothing(env) -> None:
    status, body = _call(env, _ONE, "DELETE", _request("admin", "javis", query={"working_dir": "shred"}))
    assert status == 400, body
    assert "javis" in env.store.rows


def test_deleting_one_profile_leaves_the_others_folder_alone(env) -> None:
    _seed(_default(env, "javis"))
    admin = _seed(_default(env, "admin"))
    status, _ = _call(env, _ONE, "DELETE", _request("admin", "javis", query={"working_dir": "delete"}))
    assert status == 200
    assert (admin / "notes.txt").exists()
