"""Tests for the status-file helpers used by the detached upgrade flow."""

from __future__ import annotations

import pytest

from app.upgrade import status


@pytest.fixture(autouse=True)
def _isolate_working_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the status file at a per-test tmp dir.

    Without this every test would clobber the real ``~/.cremind/.upgrade.status.json``
    of the developer running the suite.
    """
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))


def test_read_returns_idle_when_no_file() -> None:
    state = status.read()
    assert state["phase"] == "idle"
    assert state["log_tail"] == []
    assert state["upgrade_id"] is None


def test_begin_seeds_initial_state() -> None:
    state = status.begin(current_version="0.1.9", target_version="0.1.10")
    assert state["phase"] == "queued"
    assert state["current_version"] == "0.1.9"
    assert state["target_version"] == "0.1.10"
    assert state["started_at"] is not None
    assert state["finished_at"] is None
    # Round-trip through the file
    persisted = status.read()
    assert persisted["target_version"] == "0.1.10"


def test_is_running_only_true_for_active_phases() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    assert status.is_running() is True

    status.update_phase("install", "pip install cremind==0.1.10")
    assert status.is_running() is True

    status.finish(ok=True, exit_code=0)
    assert status.is_running() is False


def test_finish_failed_sets_error_and_clears_running() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.finish(ok=False, exit_code=1, error="boom")
    state = status.read()
    assert state["phase"] == "failed"
    assert state["ok"] is False
    assert state["error"] == "boom"
    assert state["finished_at"] is not None
    assert status.is_running() is False


def test_append_log_trims_to_cap() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    for i in range(status.LOG_TAIL_MAX + 50):
        status.append_log(f"line {i}")
    state = status.read()
    assert len(state["log_tail"]) == status.LOG_TAIL_MAX
    # Trim drops oldest, keeps newest
    assert state["log_tail"][-1] == f"line {status.LOG_TAIL_MAX + 49}"


def test_clear_if_terminal_drops_done_after_grace() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.finish(ok=True, exit_code=0)
    assert status.status_path().is_file()
    # Backdate finished_at past the grace window so the boot hook can
    # collect it. The realistic case for collection is a fresh boot
    # well after the previous upgrade finished.
    state = status.read()
    state["finished_at"] = state["finished_at"] - (status.TERMINAL_GRACE_S + 1)
    status.write(state)
    status.clear_if_terminal()
    assert not status.status_path().is_file()


def test_clear_if_terminal_preserves_recent_done() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.finish(ok=True, exit_code=0)
    status.clear_if_terminal()
    # Within the grace window the renderer's first poll after a backend
    # restart still needs to observe ``done`` to transition the UI.
    assert status.status_path().is_file()
    assert status.read()["phase"] == "done"


def test_clear_if_terminal_preserves_in_flight() -> None:
    status.begin(current_version="0.1.9", target_version="0.1.10")
    status.update_phase("install", "Installing...")
    status.clear_if_terminal()
    # In-flight state must NOT be wiped — a crashed runner needs to be
    # observable from the new boot.
    assert status.status_path().is_file()
    assert status.read()["phase"] == "install"


def test_read_corrupt_file_returns_idle(tmp_path) -> None:
    path = status.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{", encoding="utf-8")
    state = status.read()
    assert state["phase"] == "idle"
    assert state["log_tail"] == []
