"""Unit tests for the detached upgrade subprocess's callback wiring.

The main() entry point isn't covered here (its end-to-end behaviour
is exercised by the API spawn test in test_api.py). What we lock in
is the failure-message capture in :func:`app.upgrade.detached._make_callback`,
which surfaces the real reason for a failed upgrade instead of the
hardcoded ``"upgrade rolled back"`` string.
"""

from __future__ import annotations

import pytest

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
