"""A blueprint import's profile gets — and gives back — its own working directory.

``create_target_profile`` / ``delete_target_profile`` (abort rollback) bypass
``POST /api/profiles`` and ``DELETE /api/profiles/{name}``, so they must do
what those do for the folder: provision the new profile's own (never a folder
someone left files in), tell the change listeners, and on rollback archive a
Cremind-made folder so a later profile of that name starts empty. Two profiles
throughout: the admin's folder must come through untouched.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import working_dirs as wd

cfg = importlib.import_module("app.config.settings")


class _Store:
    def __init__(self, rows):
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


class _Profiles:
    """ConversationStorage's profile rows, backed by the working-dir store."""

    def __init__(self, store: _Store, *, delete_ok: bool = True):
        self.store = store
        self.delete_ok = delete_ok

    async def profile_exists(self, name):
        return name in self.store.rows

    async def create_profile(self, name):
        self.store.rows[name] = None
        return {"name": name}

    async def delete_profile(self, name):
        if not self.delete_ok:
            return False
        return self.store.rows.pop(name, "missing") != "missing"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    store = _Store({"admin": None})
    monkeypatch.setattr(cfg, "_dynamic_config_storage", store)
    wd.invalidate()

    import app.tools.builtin.exec_shell_autostart as autostart

    async def _teardown_processes(directory, *, profile):
        return {"stopped": [], "removed_autostart": 0}

    monkeypatch.setattr(autostart, "teardown_processes_for_dir", _teardown_processes)
    heard: list[str] = []
    wd.add_change_listener(heard.append)
    admin = Path(cfg.get_user_working_directory("admin"))
    (admin / "keep.txt").write_text("admin's")
    try:
        yield SimpleNamespace(sys=sysdir, store=store, heard=heard, admin=admin, ws=Path(wd.workspaces_root()))
    finally:
        wd.remove_change_listener(heard.append)
        wd.invalidate()


def _deps(store, **kw):
    from app.blueprint.apply import Deps

    return Deps(registry=None, conversation_storage=_Profiles(store, **kw), config_storage=None)


def _create(env, name, **kw):
    from app.blueprint.apply import create_target_profile

    session = SimpleNamespace(target_profile=None)
    asyncio.run(create_target_profile(session, name, _deps(env.store, **kw)))
    return session


def test_an_imported_profile_gets_its_own_folder_and_is_announced(env) -> None:
    session = _create(env, "imported")
    assert session.target_profile == "imported"
    folder = env.ws / "imported"
    assert folder.is_dir(), "created, like POST /api/profiles does"
    assert env.store.rows["imported"] is None, "the default"
    assert "imported" in env.heard, "the change listeners hear of it"
    assert wd.is_foreign(str(folder / "x.txt"), "admin")
    assert not wd.is_foreign(str(env.admin / "keep.txt"), "admin")


def test_an_imported_profile_never_adopts_a_folder_with_files(env) -> None:
    left = env.ws / "imported"
    left.mkdir(parents=True)
    (left / "old.txt").write_text("someone's")
    _create(env, "imported")
    assert env.store.rows["imported"] == str(env.ws / "imported-2")
    assert (env.ws / "imported-2").is_dir()
    assert (left / "old.txt").read_text() == "someone's", "left where it was"
    assert wd.is_foreign(str(env.ws / "imported-2" / "x"), "admin")


def test_rollback_archives_the_folder_and_announces_it(env) -> None:
    from app.blueprint.apply import delete_target_profile

    _create(env, "imported")
    folder = env.ws / "imported"
    (folder / "work.txt").write_text("imported's")
    env.heard.clear()
    asyncio.run(delete_target_profile("imported", _deps(env.store)))
    assert "imported" not in env.store.rows
    assert env.heard == ["imported"]
    assert not folder.exists(), "moved out of the way"
    archived = [p for p in (env.ws / wd.DELETED_DIRNAME).iterdir() if p.name.startswith("imported-")]
    assert len(archived) == 1 and (archived[0] / "work.txt").read_text() == "imported's", "kept, not deleted"
    assert (env.admin / "keep.txt").read_text() == "admin's"
    # A later profile of that name starts empty.
    _create(env, "imported")
    assert env.store.rows["imported"] is None and not any((env.ws / "imported").iterdir())


def test_a_rollback_whose_delete_failed_leaves_the_folder(env) -> None:
    from app.blueprint.apply import delete_target_profile

    _create(env, "imported")
    folder = env.ws / "imported"
    (folder / "work.txt").write_text("imported's")
    env.heard.clear()
    asyncio.run(delete_target_profile("imported", _deps(env.store, delete_ok=False)))
    assert (folder / "work.txt").exists()
    assert env.heard == []


def test_rollback_never_touches_the_admins_folder(env) -> None:
    from app.blueprint.apply import delete_target_profile

    asyncio.run(delete_target_profile("admin", _deps(env.store)))
    assert "admin" in env.store.rows
    assert (env.admin / "keep.txt").read_text() == "admin's"
    assert os.path.isdir(env.admin)
