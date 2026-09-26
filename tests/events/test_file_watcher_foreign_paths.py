"""A file watcher never reports another profile's working directory.

Registering a watcher inside another profile's folder is refused up front, but
a watcher on a PARENT folder still sees a child that belongs to someone else —
the admin whose (legacy) folder holds the workspaces root, where every other
profile's default folder lives. The watchdog handler therefore judges every
event for the watching profile and drops what is not its to see; a move across
that boundary reads as the half it may see (out of view → deleted, into view →
created). The admin is not exempt.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
from pathlib import Path

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

import app.events.file_watcher_manager as fwm
from app.config import working_dirs as wd

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


@pytest.fixture
def folders(tmp_path: Path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    admin_dir = tmp_path / "Documents"
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setenv(wd.WORKSPACES_ENV, str(admin_dir / "workspaces"))
    monkeypatch.setattr(cfg, "_dynamic_config_storage", _Store({"admin": str(admin_dir), "bob": None}))
    wd.invalidate()
    admin = Path(cfg.get_user_working_directory("admin"))
    bob = Path(cfg.get_user_working_directory("bob"))
    assert bob.parent.parent == admin
    yield admin, bob
    wd.invalidate()


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    thread = threading.Thread(target=lp.run_forever, daemon=True)
    thread.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    thread.join(timeout=5)
    lp.close()


def _deliveries(loop, profile: str, root: Path, events) -> list[dict]:
    """Feed watchdog events through the handler's real ``_dispatch`` and
    collect what it hands on to the fan-out stage."""
    seen: list[dict] = []
    handler = fwm._SharedHandler(profile=profile, root_path=str(root), recursive=True, loop=loop)

    async def _record(payload):
        seen.append(payload)

    handler._coalesce_or_fan_out = _record  # type: ignore[method-assign]
    for event in events:
        handler._dispatch(event)
    # FIFO: once this has run, every coroutine scheduled before it has too.
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)
    return seen


def test_admin_watcher_skips_a_profile_folder_inside_its_own(folders, loop):
    admin, bob = folders
    seen = _deliveries(loop, "admin", admin, [
        FileCreatedEvent(str(admin / "own.txt")),
        FileCreatedEvent(str(bob / "secret.txt")),
        FileModifiedEvent(str(bob / "notes" / "plan.md")),
        # A stray entry under the workspaces root belongs to nobody.
        FileCreatedEvent(str(admin / "workspaces" / ".deleted" / "carol-1" / "x.txt")),
    ])
    assert [(p["event_type"], p["path"]) for p in seen] == [("created", str(admin / "own.txt"))]


def test_a_move_across_the_boundary_reads_as_the_visible_half(folders, loop):
    admin, bob = folders
    seen = _deliveries(loop, "admin", admin, [
        FileMovedEvent(str(admin / "out.txt"), str(bob / "out.txt")),
        FileMovedEvent(str(bob / "in.txt"), str(admin / "in.txt")),
        FileMovedEvent(str(bob / "a.txt"), str(bob / "b.txt")),
        FileMovedEvent(str(admin / "c.txt"), str(admin / "d.txt")),
    ])
    got = [(p["event_type"], p["path"], p.get("src_path"), p.get("dest_path")) for p in seen]
    assert got == [
        ("deleted", str(admin / "out.txt"), None, None),
        ("created", str(admin / "in.txt"), None, None),
        ("moved", str(admin / "c.txt"), str(admin / "c.txt"), str(admin / "d.txt")),
    ]
    # Nothing that names a path in bob's folder reached the admin.
    assert not any(str(bob) in str(v) for p in seen for v in p.values())


def test_the_owner_still_sees_its_own_folder(folders, loop):
    admin, bob = folders
    seen = _deliveries(loop, "bob", bob, [
        FileCreatedEvent(str(bob / "secret.txt")),
        FileMovedEvent(str(bob / "a.txt"), str(bob / "b.txt")),
    ])
    assert [p["event_type"] for p in seen] == ["created", "moved"]


def test_a_watcher_left_inside_a_folder_that_became_foreign_goes_quiet(folders, loop, tmp_path):
    # Registered while the folder was bob's own; the admin has since pointed
    # bob elsewhere and given that folder to carol. bob's watcher must not
    # keep reporting carol's files.
    store = importlib.import_module("app.config.settings")._dynamic_config_storage
    work = tmp_path / "work"
    work.mkdir()
    store.rows["bob"] = str(work)
    wd.invalidate()
    assert len(_deliveries(loop, "bob", work, [FileCreatedEvent(str(work / "a.txt"))])) == 1
    store.rows["bob"] = str(tmp_path / "work2")
    store.rows["carol"] = str(work)
    wd.invalidate()
    seen = _deliveries(loop, "bob", work, [FileCreatedEvent(str(work / "new.txt"))])
    assert seen == []


def test_a_failed_ownership_check_drops_the_event(folders, loop, monkeypatch):
    admin, _bob = folders

    def _boom(path, profile):
        raise RuntimeError("storage exploded")

    monkeypatch.setattr(wd, "is_foreign", _boom)
    assert _deliveries(loop, "admin", admin, [FileCreatedEvent(str(admin / "x.txt"))]) == []
