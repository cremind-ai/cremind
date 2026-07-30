"""Upgrade cleanup for builtin skills that dropped shipped files.

``copytree`` overwrites but never deletes, so a file a release removed would keep
running in every existing profile. These tests pin both halves of that: the
tombstoned files go, and the user's own state stays.
"""

from __future__ import annotations

import asyncio

import pytest

import app.skills.sync as sync


@pytest.fixture
def profile_tree(tmp_path, monkeypatch):
    """A builtin source tree plus an already-installed profile copy."""
    builtin = tmp_path / "builtin"
    (builtin / "gmail" / "scripts" / "app" / "google").mkdir(parents=True)
    (builtin / "gmail" / "SKILL.md").write_text("name: gmail", encoding="utf-8")
    (builtin / "gmail" / "scripts" / "app" / "cli.py").write_text("new", encoding="utf-8")

    profiles = tmp_path / "profiles"
    installed = profiles / "alice" / "skills" / "gmail"
    (installed / "scripts" / "app" / "google").mkdir(parents=True)
    (installed / "events" / "new_email").mkdir(parents=True)

    # Files this release removed.
    (installed / "scripts" / "event_listener.py").write_text("old", encoding="utf-8")
    (installed / "scripts" / "app" / "listener.py").write_text("old", encoding="utf-8")
    (installed / "scripts" / "app" / "google" / "relay_client.py").write_text("old", encoding="utf-8")
    # Per-profile state that must survive.
    (installed / "scripts" / ".env").write_text("USERNAME=x", encoding="utf-8")
    (installed / "scripts" / ".google_token.json").write_text("{}", encoding="utf-8")
    (installed / "scripts" / ".listener_state.json").write_text("{}", encoding="utf-8")
    (installed / "scripts" / ".listener_heartbeat").write_text("1", encoding="utf-8")
    (installed / "events" / "new_email" / "pending.md").write_text("mail", encoding="utf-8")

    monkeypatch.setattr(sync, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(
        sync, "profile_skills_dir", lambda profile: profiles / profile / "skills"
    )
    return installed


def test_obsolete_files_are_removed(profile_tree):
    sync.sync_builtin_skills_into_profile("alice")
    assert not (profile_tree / "scripts" / "event_listener.py").exists()
    assert not (profile_tree / "scripts" / "app" / "listener.py").exists()
    assert not (profile_tree / "scripts" / "app" / "google" / "relay_client.py").exists()


def test_shipped_files_are_still_refreshed(profile_tree):
    sync.sync_builtin_skills_into_profile("alice")
    assert (profile_tree / "scripts" / "app" / "cli.py").read_text(encoding="utf-8") == "new"


def test_per_profile_state_is_untouched(profile_tree):
    sync.sync_builtin_skills_into_profile("alice")
    scripts = profile_tree / "scripts"
    assert scripts.joinpath(".env").read_text(encoding="utf-8") == "USERNAME=x"
    assert scripts.joinpath(".google_token.json").exists()
    assert scripts.joinpath(".listener_state.json").exists()
    assert scripts.joinpath(".listener_heartbeat").exists()
    # The event folder and any queued events belong to the user, not the release.
    assert profile_tree.joinpath("events", "new_email", "pending.md").exists()


def test_cleanup_is_idempotent(profile_tree):
    sync.sync_builtin_skills_into_profile("alice")
    sync.sync_builtin_skills_into_profile("alice")  # must not raise on missing files
    assert not (profile_tree / "scripts" / "event_listener.py").exists()


def test_a_fresh_install_is_unaffected(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    (builtin / "gmail" / "scripts").mkdir(parents=True)
    (builtin / "gmail" / "SKILL.md").write_text("name: gmail", encoding="utf-8")
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(sync, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(
        sync, "profile_skills_dir", lambda profile: profiles / profile / "skills"
    )
    assert sync.sync_builtin_skills_into_profile("bob") == ["gmail"]
    assert (profiles / "bob" / "skills" / "gmail" / "SKILL.md").exists()


def test_retired_listener_autostart_is_deregistered(profile_tree, monkeypatch):
    """A row pointing at a deleted listener would fail to spawn on every boot."""
    calls: list[tuple[str, str]] = []

    async def fake_teardown(directory, *, profile):
        calls.append((directory.name, profile))
        return {"stopped": [], "removed_autostart": 1}

    monkeypatch.setattr(
        "app.tools.builtin.exec_shell_autostart.teardown_processes_for_dir", fake_teardown
    )
    asyncio.run(sync._retire_listener_autostarts("alice"))
    assert calls == [("gmail", "alice")]


def test_retirement_never_blocks_boot(profile_tree, monkeypatch):
    async def boom(directory, *, profile):
        raise RuntimeError("storage down")

    monkeypatch.setattr(
        "app.tools.builtin.exec_shell_autostart.teardown_processes_for_dir", boom
    )
    asyncio.run(sync._retire_listener_autostarts("alice"))  # swallowed


def test_retirement_skips_absent_skill_dirs(tmp_path, monkeypatch):
    called = False

    async def fake_teardown(directory, *, profile):
        nonlocal called
        called = True
        return {"stopped": [], "removed_autostart": 0}

    monkeypatch.setattr(
        sync, "profile_skills_dir", lambda profile: tmp_path / profile / "skills"
    )
    monkeypatch.setattr(
        "app.tools.builtin.exec_shell_autostart.teardown_processes_for_dir", fake_teardown
    )
    asyncio.run(sync._retire_listener_autostarts("nobody"))
    assert called is False
