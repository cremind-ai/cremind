"""RootGuard: every way a root can look gone, and the thresholds for mass deletion.

The guard only ever holds. These tests pin the conditions under which it
does, because a false "ok" on an unmounted root is what would let a scan wipe
an index.
"""

from __future__ import annotations

import os

import pytest

from app.userdocs import deploy_env
from app.userdocs.discovery.guard import DeleteBurst, RootGuard
from app.userdocs.discovery.walker import fit_i63


@pytest.fixture
def guard() -> RootGuard:
    return RootGuard()


def test_ok_root(guard, tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    v = guard.check_root(str(tmp_path), manifest_count=100, root_identity=guard.identity_of(str(tmp_path)))
    assert v.ok and v.reason is None


def test_missing_root(guard, tmp_path):
    v = guard.check_root(str(tmp_path / "nope"), manifest_count=0, root_identity=None)
    assert not v.ok and v.reason == "root_missing" and v.detail["exists"] is False


def test_a_file_is_not_a_root(guard, tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    v = guard.check_root(str(f), manifest_count=0, root_identity=None)
    assert not v.ok and v.reason == "root_missing" and v.detail["exists"] is True


def test_empty_root_holds_only_when_the_index_expected_files(guard, tmp_path):
    v = guard.check_root(str(tmp_path), manifest_count=20, root_identity={"seen_nonempty": True})
    assert not v.ok and v.reason == "root_empty" and v.detail["seen_nonempty"] is True
    assert guard.check_root(str(tmp_path), manifest_count=19, root_identity=None).ok


def test_device_change_is_advisory(guard, tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    real_dev = fit_i63(os.stat(tmp_path).st_dev)
    v = guard.check_root(str(tmp_path), manifest_count=50, root_identity={"dev": real_dev + 1})
    assert v.ok and v.reason == "device_changed"
    assert v.detail == {"old_dev": real_dev + 1, "new_dev": real_dev}
    assert guard.device_change_holds(missing=21, total=100)
    assert not guard.device_change_holds(missing=20, total=100)
    assert not guard.device_change_holds(missing=0, total=0)


def test_bind_missing_in_a_container(guard, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    status = {"root_mounted": False, "snippet": '- "~/Documents:/root/Documents"'}
    monkeypatch.setattr(deploy_env, "docker_root_status", lambda root: dict(status))
    v = guard.check_root(str(tmp_path), manifest_count=0, root_identity=None, docker_bind_expected=True)
    assert not v.ok and v.reason == "bind_missing" and v.detail["snippet"]
    # Not expected -> not checked; unknown (None) -> not a hold.
    assert guard.check_root(str(tmp_path), manifest_count=0, root_identity=None).ok
    status["root_mounted"] = None
    assert guard.check_root(str(tmp_path), manifest_count=0, root_identity=None, docker_bind_expected=True).ok


def test_bind_missing_outranks_root_empty(guard, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_env, "docker_root_status", lambda root: {"root_mounted": False})
    v = guard.check_root(str(tmp_path), manifest_count=500, root_identity=None, docker_bind_expected=True)
    assert v.reason == "bind_missing"


@pytest.mark.parametrize("missing,total,events,expected", [
    (200, 400, 0, "mass_delete"),
    (199, 200, 0, None),       # below the absolute floor
    (300, 1000, 0, None),      # below half
    (500, 999, 0, "mass_delete"),
    (0, 10, 501, "mass_delete"),
    (0, 10, 500, None),
    (0, 0, 0, None),
])
def test_check_bulk(guard, missing, total, events, expected):
    assert guard.check_bulk(missing, total, delete_events_in_window=events) == expected


def test_identity_of(guard, tmp_path):
    ident = guard.identity_of(str(tmp_path))
    assert set(ident) == {"dev", "ino", "realpath", "fstype", "is_mountpoint", "seen_nonempty"}
    assert ident["seen_nonempty"] is False and ident["is_mountpoint"] is False
    assert ident["dev"] == fit_i63(os.stat(tmp_path).st_dev)
    (tmp_path / "x").write_text("x", encoding="utf-8")
    assert guard.identity_of(str(tmp_path))["seen_nonempty"] is True
    assert guard.identity_of(str(tmp_path / "gone"))["dev"] is None


def test_delete_burst_window():
    burst = DeleteBurst(window_s=10)
    assert burst.record(300, now=100.0) == 300
    assert burst.record(250, now=105.0) == 550
    assert burst.count(now=109.9) == 550
    assert burst.count(now=110.5) == 250  # the first batch aged out
    assert burst.count(now=200.0) == 0
