"""Tests for how the detached restore runner stops the server.

Phase 1 stages the restore while the server runs, then has to stop it so the
next boot can swap the DB and file trees out. Stopping it *gracefully* is what
guarantees there are no open handles on those trees when the new process
arrives — a terminated server leaves watchers and sidecars holding them, which
on Windows is the difference between a restore and a sharing violation.

Mirrors tests/upgrade/test_detached.py: the same handshake, a different
``source`` and status file.
"""

from __future__ import annotations

import json
import signal

import pytest

from app.backup import detached
from app.backup.status import restore_status
from app.system.shutdown_watch import shutdown_request_path


@pytest.fixture(autouse=True)
def _isolate_system_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    restore_status.begin(detail={"archive": "test.cremind-backup"})


def test_stop_parent_does_nothing_without_a_pid(tmp_path) -> None:
    detached._stop_parent(0)

    assert not shutdown_request_path(tmp_path).exists()


def test_stop_parent_asks_gracefully_and_leaves_it_at_that(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signalled: list[int] = []

    monkeypatch.setattr(detached, "wait_for_parent_exit", lambda pid, timeout_s: True)
    monkeypatch.setattr(detached.os, "kill", lambda pid, sig: signalled.append(pid))

    detached._stop_parent(4242)

    payload = json.loads(
        shutdown_request_path(tmp_path).read_text(encoding="utf-8")
    )
    assert payload["source"] == "restore"
    assert signalled == []


def test_stop_parent_falls_back_to_a_signal_when_the_wait_runs_out(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    """The staging succeeded and the marker is written; the operator only
    needs to know the restart is theirs to do."""

    def _denied(pid, sig):
        raise PermissionError("not yours")

    monkeypatch.setattr(detached, "wait_for_parent_exit", lambda pid, timeout_s: False)
    monkeypatch.setattr(detached.os, "kill", _denied)

    detached._stop_parent(4242)

    state = restore_status.read()
    assert any("permission denied" in line for line in state["log_tail"])
