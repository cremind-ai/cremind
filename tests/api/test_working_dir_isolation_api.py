"""REST surfaces that resolve a profile's working directory — with two profiles.

Terminals, file watchers and ``/api/me`` each resolve "the user working
directory"; since every profile has its own, each must resolve the CALLER's,
and anything that lets a caller name a directory must refuse another profile's
(the admin is not exempt).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

pytest.importorskip("a2a")

from app.config import working_dirs as wd  # noqa: E402

cfg = importlib.import_module("app.config.settings")


class _Store:
    """The three DynamicConfigStorage methods working_dirs uses."""

    def __init__(self, rows):
        self.rows = dict(rows)

    def get_profile_working_dir(self, profile):
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile, value):
        self.rows[profile] = value
        return True

    def profile_working_dirs(self):
        return dict(self.rows)


def _real(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.fixture
def two(tmp_path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(tmp_path / "workspaces"))
    store = _Store({"admin": None, "javis": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()
    ns = SimpleNamespace(tmp=tmp_path, store=store)
    ns.admin = Path(cfg.get_user_working_directory("admin"))
    ns.javis = Path(cfg.get_user_working_directory("javis"))
    yield ns
    wd.invalidate()


def _req(username, body=None, path_params=None, headers=None):
    async def _json():
        return body if body is not None else {}

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        headers=headers or {},
        json=_json,
    )


# ── terminals ──────────────────────────────────────────────────────────────


def _terminal_create(monkeypatch) -> tuple[Callable, list]:
    import app.api.terminals as terminals

    opened = []

    async def _fake_create(profile, *, cwd, cols, rows, extra_env, **_kw):
        opened.append((profile, cwd))
        return SimpleNamespace(
            terminal_id="term-x", title="Terminal 1", shell="bash",
            working_dir=cwd, created_at=0.0,
        )

    monkeypatch.setattr(terminals, "create_terminal", _fake_create)
    monkeypatch.setattr(terminals, "build_system_env", lambda p: {})
    for route in terminals.get_terminal_routes():
        if getattr(route, "path", "") == "/api/terminals" and "POST" in (route.methods or ()):
            return route.endpoint, opened
    raise AssertionError("POST /api/terminals is gone")


def test_a_terminal_opens_in_the_callers_own_folder(two, monkeypatch):
    handler, opened = _terminal_create(monkeypatch)
    asyncio.run(handler(_req("admin", {})))
    asyncio.run(handler(_req("javis", {})))
    assert [(p, _real(c)) for p, c in opened] == [
        ("admin", _real(two.admin)), ("javis", _real(two.javis)),
    ]


@pytest.mark.parametrize("caller,other", [("javis", "admin"), ("admin", "javis")])
def test_a_terminal_never_opens_in_another_profiles_folder(two, monkeypatch, caller, other):
    """Like a cwd that does not exist: the terminal opens in the caller's own
    folder instead (a conversation's stale cwd must not fail the button)."""
    handler, opened = _terminal_create(monkeypatch)
    resp = asyncio.run(handler(_req(caller, {"cwd": str(getattr(two, other))})))
    assert resp.status_code == 201
    assert _real(opened[0][1]) == _real(getattr(two, caller))
    # An ordinary folder is still honoured.
    elsewhere = two.tmp / "elsewhere"
    elsewhere.mkdir()
    asyncio.run(handler(_req(caller, {"cwd": str(elsewhere)})))
    assert opened[1][1] == str(elsewhere)


# ── file watchers ──────────────────────────────────────────────────────────


def _fw_handler(path: str, method: str) -> Callable:
    import app.api.file_watchers as fw_api

    for route in fw_api.get_file_watcher_routes():
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


@pytest.mark.parametrize("caller,other", [("javis", "admin"), ("admin", "javis")])
def test_creating_a_watcher_on_another_profiles_folder_is_refused(two, caller, other):
    handler = _fw_handler("/api/file-watchers", "POST")
    resp = asyncio.run(handler(_req(caller, {
        "action": "report it", "path": str(getattr(two, other)),
    })))
    assert resp.status_code == 400
    body = _body(resp)
    assert body["error"] == "foreign_working_dir"
    assert "belongs to another profile" in body["message"]


def test_a_relative_watch_root_is_the_callers_own_folder(two):
    import app.api.file_watchers as fw_api

    (two.javis / "inbox").mkdir()
    root, err = fw_api._checked_watch_root("inbox", "javis")
    assert err is None and _real(root) == _real(two.javis / "inbox")
    root, err = fw_api._checked_watch_root("", "admin")
    assert err is None and _real(root) == _real(two.admin)
    root, err = fw_api._checked_watch_root("inbox", "")
    assert root is None and err.status_code == 403


def test_moving_a_watcher_into_another_profiles_folder_is_refused(two, monkeypatch):
    from a2a.server.models import Base
    import app.storage.models  # noqa: F401
    from sqlalchemy import text

    import app.api.file_watchers as fw_api
    from app.databases.sqlite import SqliteDatabaseProvider
    from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage

    provider = SqliteDatabaseProvider(str(two.tmp / "api.db"))
    eng = provider.sync_engine()
    for name in ("profiles", "channels", "conversations", "file_watcher_subscriptions"):
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p1', 'admin', 0, 0)"))
        c.execute(text(
            "INSERT INTO conversations (id, profile, title, created_at, updated_at) "
            "VALUES ('c1', 'admin', 't', 0, 0)"))
    store = FileWatcherSubscriptionStorage(provider)
    monkeypatch.setattr(fw_api, "get_file_watcher_storage", lambda *a, **k: store)
    monkeypatch.setattr(fw_api, "publish_file_watchers_admin_changed", lambda *a, **k: None)
    row = store.insert(
        conversation_id="c1", profile="admin", name="w", root_path=str(two.admin),
        recursive=True, target_kind="any", event_types="created",
        extensions="", action="do a thing",
    )

    handler = _fw_handler("/api/file-watchers/{id}", "PATCH")
    resp = asyncio.run(handler(_req(
        "admin", {"path": str(two.javis)}, path_params={"id": row["id"]},
    )))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "foreign_working_dir"
    assert store.get(row["id"])["root_path"] == str(two.admin)


# ── /api/me ────────────────────────────────────────────────────────────────


def test_me_reports_the_callers_own_folder_and_whether_it_is_the_default(two, monkeypatch):
    import app.api.tokens as tokens_api

    monkeypatch.setattr(
        tokens_api, "verify_token", lambda tok: {"sub": tok, "profile": tok},
    )

    def _me(profile):
        return _body(asyncio.run(tokens_api.get_me(_req(
            profile, headers={"Authorization": f"Bearer {profile}"},
        ))))

    admin, javis = _me("admin"), _me("javis")
    assert _real(admin["user_working_dir"]) == _real(two.admin)
    assert _real(javis["user_working_dir"]) == _real(two.javis)
    assert admin["user_working_dir_default"] is True
    assert javis["user_working_dir_default"] is True

    chosen = two.tmp / "javis-projects"
    two.store.rows["javis"] = str(chosen)
    wd.invalidate()
    javis = _me("javis")
    assert _real(javis["user_working_dir"]) == _real(chosen)
    assert javis["user_working_dir_default"] is False
    # The admin's answer did not move with it.
    assert _real(_me("admin")["user_working_dir"]) == _real(two.admin)
