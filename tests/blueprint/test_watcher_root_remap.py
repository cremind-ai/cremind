"""Blueprint import: a file watcher rooted in the SOURCE profile's own working
directory lands in the TARGET profile's.

Each profile has its own working directory, so the plain prefix relocation
(``<source SYS>`` → ``<target SYS>``) would carry
``<workspaces>/<source profile>/inbox`` to the target's copy of the SOURCE
profile's folder — another profile's, which the importing profile may not
watch. Two profiles on the target (``admin`` + ``javis``) so "another
profile's" is real.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from app.backup.paths import build_path_map
from app.blueprint.manifest import BlueprintManifest, SourcePaths
from app.blueprint.plan import (
    _plan_events,
    load_payload_manifest,
    relocate_watcher_root,
    remap_into_target_working_dir,
)
from app.config import working_dirs as wd

cfg = importlib.import_module("app.config.settings")


class _Store:
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


@pytest.fixture
def target(tmp_path, monkeypatch):
    sysdir = tmp_path / "sys"
    sysdir.mkdir()
    monkeypatch.setattr(cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(wd.WORKSPACES_ENV, raising=False)
    monkeypatch.setattr(cfg, "_dynamic_config_storage", _Store({"admin": None, "javis": None}))
    wd.invalidate()
    yield sysdir
    wd.invalidate()


def _windows_manifest(**paths) -> BlueprintManifest:
    sp = {
        "system_dir": r"C:\Users\alice\.cremind",
        "home_dir": r"C:\Users\alice",
        "user_working_dir": r"C:\Users\alice\.cremind\workspaces\alice",
        "sep": "\\",
        "case_insensitive": True,
    }
    sp.update(paths)
    return BlueprintManifest(
        app_version="0.0.0", platform="win32", source_profile="alice",
        source_paths=SourcePaths(**sp),
    )


def test_a_root_in_the_source_profiles_workspace_lands_in_the_targets(target):
    m = _windows_manifest()
    got = remap_into_target_working_dir(
        m, r"c:\users\alice\.cremind\workspaces\ALICE\inbox\new", "javis",
    )
    assert _real(got) == _real(target / "workspaces" / "javis" / "inbox" / "new")
    # The workspace root itself maps to the target's folder itself.
    got = remap_into_target_working_dir(m, r"C:\Users\alice\.cremind\workspaces\alice", "admin")
    assert _real(got) == _real(target / "workspaces" / "admin")


def test_a_legacy_blueprint_maps_its_server_wide_folder(target):
    """Older blueprints recorded the one server-wide folder (every profile's)."""
    m = _windows_manifest(user_working_dir=r"C:\Users\alice\Documents")
    got = remap_into_target_working_dir(m, r"C:\Users\alice\Documents\notes", "javis")
    assert _real(got) == _real(target / "workspaces" / "javis" / "notes")


def test_a_container_source_maps_through_its_workspaces_root(target):
    m = BlueprintManifest(
        app_version="0.0.0", platform="linux", source_profile="alice",
        source_paths=SourcePaths(
            system_dir="/root/.cremind", home_dir="/root", user_working_dir="",
            sep="/", case_insensitive=False, workspaces_root="/root/Documents/cremind-workspaces",
        ),
    )
    got = remap_into_target_working_dir(m, "/root/Documents/cremind-workspaces/alice/x", "javis")
    assert _real(got) == _real(target / "workspaces" / "javis" / "x")
    got = remap_into_target_working_dir(m, "/root/Documents/cremind-workspaces/alice", "admin")
    assert _real(got) == _real(target / "workspaces" / "admin")
    # Case matters on a POSIX source.
    assert remap_into_target_working_dir(m, "/root/Documents/cremind-workspaces/Alice/x", "javis") is None
    # The user's own ``workspaces`` folder next to it is nobody's workspace.
    assert remap_into_target_working_dir(m, "/root/Documents/workspaces/alice/x", "javis") is None


def test_other_roots_keep_the_usual_relocation(target, tmp_path):
    m = _windows_manifest()
    pm = build_path_map(m, str(target), str(tmp_path / "home"))
    got, changed = relocate_watcher_root(m, pm, r"C:\Users\alice\Projects\app", "javis")
    assert changed and _real(got) == _real(tmp_path / "home" / "Projects" / "app")
    # Unmapped stays as is.
    got, changed = relocate_watcher_root(m, pm, r"D:\data", "javis")
    assert (got, changed) == (r"D:\data", False)


def test_the_plan_flags_a_suggestion_inside_another_profiles_folder(target, tmp_path):
    """A source watcher on ANOTHER source profile's workspace relocates (by the
    system-dir rule) into the same-named folder here — which belongs to a
    different profile on this install, so the plan says so."""
    m = _windows_manifest()
    data = {"file_watcher": [
        {"name": "mine", "root_path": r"C:\Users\alice\.cremind\workspaces\alice\in"},
        {"name": "theirs", "root_path": r"C:\Users\alice\.cremind\workspaces\admin\in"},
    ]}
    step = _plan_events(data, m, [], target_profile="javis")
    reqs = {r["name"]: r for r in step["requirements"]}
    assert _real(reqs["mine"]["suggested_root_path"]) == _real(target / "workspaces" / "javis" / "in")
    assert reqs["mine"]["foreign"] is False
    assert reqs["theirs"]["foreign"] is True
    assert reqs["theirs"]["exists"] is False


def test_apply_reads_the_full_manifest_from_the_payload(tmp_path):
    """The session keeps only the summary, which has no source roots."""
    m = _windows_manifest()
    (tmp_path / "manifest.json").write_text(json.dumps(m.to_dict()), encoding="utf-8")
    loaded = load_payload_manifest(tmp_path)
    assert loaded is not None
    assert loaded.source_paths.user_working_dir == m.source_paths.user_working_dir
    assert "source_paths" not in m.summary()
    assert load_payload_manifest(tmp_path / "missing") is None
