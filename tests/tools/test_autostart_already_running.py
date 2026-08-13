"""A skill listener that exits because another instance holds its lock is
*already running*, not a failed spawn.

The gcalendar/gdrive/jira listeners guard themselves with an exclusive OS lock
on ``scripts/.listener.lock``. When one is already up, a second spawn prints
"another <x> listener is already running for this skill" and exits 1. Retrying
that is pointless (every attempt hits the same held lock) and escalating it to a
high-priority ``autostart_failed`` notification is actively misleading — the
listener the user wants IS running.

Regression for a boot log that showed three consecutive failed retries plus an
alert while a healthy (orphaned) listener was serving events the whole time.

Tests use ``asyncio.run`` per the repo idiom (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.tools.builtin import exec_shell_autostart as autostart
from app.tools.builtin.exec_shell_autostart import ALREADY_RUNNING

_LOCK_STDERR = (
    "2026-08-13 11:37:08,262 ERROR gcalendar - another calendar listener is "
    "already running for this skill (lock: C:\\x\\scripts\\.listener.lock); "
    "exiting to avoid duplicate event files"
)


class _DeadProc:
    """A process that has already exited, as _sanity_check would find it."""

    returncode = 1
    pid = 4242

    async def wait(self):
        return 1


def _row(**over):
    row = {
        "id": "row-1",
        "profile": "admin",
        "command": "uv run scripts/event_listener.py",
        "working_dir": ".",
        "is_pty": False,
    }
    row.update(over)
    return row


def _spawn(stderr: str, *, spawns: list):
    """Run spawn_from_autostart against a process that dies with *stderr*."""

    async def _fake_spawn(*args, **kwargs):
        spawns.append(1)
        return _DeadProc()

    with patch.object(autostart, "_spawn_command", _fake_spawn), \
         patch.object(autostart, "_drain_early_output", _drain(stderr)), \
         patch.object(autostart, "build_system_env", lambda p: {}):
        return asyncio.run(
            autostart.spawn_from_autostart(
                _row(), base_delay=0.01, sanity_delay=0.01,
            )
        )


def _drain(stderr: str):
    async def _f(proc):
        return "", stderr
    return _f


def test_lock_conflict_returns_already_running_without_retrying():
    spawns: list = []
    process_id, error = _spawn(_LOCK_STDERR, spawns=spawns)

    assert process_id is None
    assert error is ALREADY_RUNNING
    assert len(spawns) == 1, "a held lock must not be retried — it can't clear"


def test_other_failures_still_retry_and_report():
    spawns: list = []
    process_id, error = _spawn("ModuleNotFoundError: no module named 'foo'", spawns=spawns)

    assert process_id is None
    assert error is not ALREADY_RUNNING
    assert "exit code 1" in error
    assert len(spawns) == 3, "genuine failures keep the existing 3-attempt retry"


def test_already_running_marker_matches_every_lock_based_listener():
    """gcalendar/gdrive/jira all print the same phrase; keep them all covered."""
    for skill in ("calendar", "drive", "jira"):
        msg = f"another {skill} listener is already running for this skill (lock: x)"
        assert autostart._is_already_running("", msg)

    assert not autostart._is_already_running("", "connection refused")
    assert not autostart._is_already_running("", "")


def test_already_running_is_not_escalated_to_a_notification():
    """_spawn_one must neither mark the row failed nor raise a priority alert."""
    calls = {"set_error": 0, "clear_error": 0, "notify": 0}

    class _Storage:
        def set_error(self, *a, **k):
            calls["set_error"] += 1

        def clear_error(self, *a, **k):
            calls["clear_error"] += 1

    async def _fake(row, **kwargs):
        return None, ALREADY_RUNNING

    with patch.object(autostart, "spawn_from_autostart", _fake), \
         patch.object(autostart, "_notify_autostart_failure",
                      lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1)):
        asyncio.run(autostart._spawn_one(_Storage(), _row()))

    assert calls["notify"] == 0, "an already-running listener is not an alert"
    assert calls["set_error"] == 0, "the row must not be marked failed"
    assert calls["clear_error"] == 1, "any stale error should be cleared"


def test_genuine_failure_is_still_escalated():
    calls = {"set_error": 0, "notify": 0}

    class _Storage:
        def set_error(self, *a, **k):
            calls["set_error"] += 1

        def clear_error(self, *a, **k):
            pass

    async def _fake(row, **kwargs):
        return None, "process exited immediately — exit code 127"

    with patch.object(autostart, "spawn_from_autostart", _fake), \
         patch.object(autostart, "publish_process_list_changed", lambda *a, **k: None), \
         patch.object(autostart, "_notify_autostart_failure",
                      lambda *a, **k: calls.__setitem__("notify", calls["notify"] + 1)):
        asyncio.run(autostart._spawn_one(_Storage(), _row()))

    assert calls["notify"] == 1
    assert calls["set_error"] == 1


def test_already_running_sentinel_stays_truthy():
    """Call sites written as `if error:` predate the sentinel and must keep
    treating it as a non-empty error slot."""
    assert bool(ALREADY_RUNNING)
    assert isinstance(ALREADY_RUNNING, str)
