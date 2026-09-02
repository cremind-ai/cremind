"""Unit tests for the detached upgrade subprocess's callback and stop wiring.

The main() entry point isn't covered here (its end-to-end behaviour
is exercised by the API spawn test in test_api.py). What we lock in
is the failure-message capture in :func:`app.upgrade.detached._make_callback`,
which surfaces the real reason for a failed upgrade instead of the
hardcoded ``"upgrade rolled back"`` string — and
:func:`app.upgrade.detached._stop_parent`, which has to ask the server to
stop gracefully and still guarantee it stops.
"""

from __future__ import annotations

import json
import signal

import pytest

from app.system.shutdown_watch import shutdown_request_path
from app.upgrade import detached, status
from app.upgrade.runner import UpgradeEvent


@pytest.fixture(autouse=True)
def _isolate_working_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the status file at a per-test tmp dir so update_phase / append_log
    don't clobber the developer's real ``~/.cremind/.upgrade.status.json``."""
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    # The callback writes through status.update_phase, which expects the file
    # to exist via status.begin() in the real flow. Seed it here so the unit
    # test exercises the same write path without main()'s full setup.
    status.begin(current_version="1.0.0rc2.dev1", target_version="1.0.0rc1.dev2")


def test_callback_captures_last_ok_false_message() -> None:
    cb, last_failure = detached._make_callback()

    # Mix of ok=True and ok=False events; the closure should latch onto
    # the most recent ok=False message and ignore the ok=True ones.
    cb(UpgradeEvent("check", "Resolving test release …"))
    cb(UpgradeEvent("check", "WARNING: min_supported_upgrade_from above current."))
    cb(UpgradeEvent("backup", "Snapshotting database…"))
    cb(UpgradeEvent("install", "cremind db upgrade exited with code 1.", ok=False))

    assert last_failure["message"] == "cremind db upgrade exited with code 1."


def test_callback_keeps_message_none_when_no_failures() -> None:
    cb, last_failure = detached._make_callback()

    cb(UpgradeEvent("check", "Pre-flight checks…"))
    cb(UpgradeEvent("backup", "Backup written to …"))
    cb(UpgradeEvent("done", "Upgraded to 1.0.0rc2.dev1."))

    assert last_failure["message"] is None


def test_callback_latches_most_recent_failure() -> None:
    # If multiple ok=False events fire (rare, but possible — e.g. the
    # install fails and rollback also fails), the *most recent* one
    # wins so the modal headline matches what the runner finished on.
    cb, last_failure = detached._make_callback()

    cb(UpgradeEvent("install", "pip install exited with code 1.", ok=False))
    cb(UpgradeEvent("rollback", "Rollback ALSO failed: disk full.", ok=False))

    assert last_failure["message"] == "Rollback ALSO failed: disk full."


def test_callback_writes_messages_to_log_tail() -> None:
    # The callback still flows every non-terminal event into the status
    # file's log_tail via update_phase — that path is what the renderer
    # polls. Verify it works alongside the new capture behaviour.
    cb, _last = detached._make_callback()
    cb(UpgradeEvent("install", "pip install exited with code 1.", ok=False))

    state = status.read()
    assert state["phase"] == "install"
    assert state["ok"] is False
    assert any("pip install exited with code 1." in line for line in state["log_tail"])


# ── _stop_parent ─────────────────────────────────────────────────────────


def test_stop_parent_does_nothing_without_a_pid(tmp_path) -> None:
    """``--parent-pid 0`` means "nobody to stop" (the Electron path). It must
    not leave a sentinel for the next server to trip over."""
    detached._stop_parent(0)

    assert not shutdown_request_path(tmp_path).exists()


def test_stop_parent_asks_gracefully_and_leaves_it_at_that(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentinel is the request; a server that acts on it must never also
    be signalled — that signal is the hard kill this replaced."""
    signalled: list[int] = []

    monkeypatch.setattr(detached, "wait_for_parent_exit", lambda pid, timeout_s: True)
    monkeypatch.setattr(detached.os, "kill", lambda pid, sig: signalled.append(pid))

    detached._stop_parent(4242)

    payload = json.loads(
        (tmp_path / shutdown_request_path(tmp_path).name).read_text(encoding="utf-8")
    )
    assert payload["source"] == "upgrade"
    assert signalled == []


def test_stop_parent_falls_back_to_a_signal_when_the_wait_runs_out(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers both a wedged shutdown and the first upgrade FROM a build whose
    server is too old to watch for the sentinel at all."""
    signalled: list[tuple[int, int]] = []

    monkeypatch.setattr(detached, "wait_for_parent_exit", lambda pid, timeout_s: False)
    monkeypatch.setattr(
        detached.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    detached._stop_parent(4242)

    assert signalled == [(4242, signal.SIGTERM)]


def test_stop_parent_reports_a_refused_signal_in_the_status_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade itself succeeded; the operator needs to know only the
    restart didn't."""

    def _denied(pid, sig):
        raise PermissionError("not yours")

    monkeypatch.setattr(detached, "wait_for_parent_exit", lambda pid, timeout_s: False)
    monkeypatch.setattr(detached.os, "kill", _denied)

    detached._stop_parent(4242)

    state = status.read()
    assert any("permission denied" in line for line in state["log_tail"])
