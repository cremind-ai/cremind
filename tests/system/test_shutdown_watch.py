"""Tests for the ``.shutdown.request`` sentinel and its watcher.

This is how an out-of-process initiator (the detached upgrade and restore
runners) asks a running server to stop *gracefully*. The alternative they
used before was a signal, which on Windows terminates rather than asks.

Two properties carry the design: the sentinel is consumed before it is acted
on (a crash in between must not leave a file that stops the next server the
moment it boots), and the watcher is single-shot.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from app.system import shutdown_watch
from app.system.shutdown_watch import (
    SHUTDOWN_REQUEST_FILE,
    ShutdownRequestWatcher,
    clear_stale_shutdown_request,
    shutdown_request_path,
    write_shutdown_request,
)


# Fast enough to keep the suite quick, slow enough to exercise the wait.
_POLL_S = 0.05


@pytest.fixture
def watcher():
    """Any watcher a test starts is stopped, even when the test fails."""
    made: list[ShutdownRequestWatcher] = []

    def _make(system_dir, on_request) -> ShutdownRequestWatcher:
        w = ShutdownRequestWatcher(
            system_dir, on_request, poll_interval_s=_POLL_S
        )
        made.append(w)
        return w

    yield _make
    for w in made:
        w.stop()


# ── the sentinel ─────────────────────────────────────────────────────────


def test_the_request_carries_who_asked_and_when(tmp_path) -> None:
    write_shutdown_request(tmp_path, source="upgrade")

    path = shutdown_request_path(tmp_path)
    assert path == tmp_path / SHUTDOWN_REQUEST_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == "upgrade"
    assert payload["pid"] == os.getpid()
    assert isinstance(payload["requested_at"], float)


def test_the_request_leaves_no_temp_file_behind(tmp_path) -> None:
    """It is written through a rename so a watcher can never read half of
    one; the temp must not survive as a second file in the system dir."""
    write_shutdown_request(tmp_path, source="restore")

    assert [p.name for p in tmp_path.iterdir()] == [SHUTDOWN_REQUEST_FILE]


def test_clearing_an_absent_request_is_not_an_error(tmp_path) -> None:
    clear_stale_shutdown_request(tmp_path)  # must not raise
    write_shutdown_request(tmp_path, source="upgrade")
    clear_stale_shutdown_request(tmp_path)
    assert not shutdown_request_path(tmp_path).exists()


# ── the watcher ──────────────────────────────────────────────────────────


def _wait_for(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_a_request_fires_the_callback_once_and_consumes_the_file(
    tmp_path, watcher
) -> None:
    fired = threading.Event()
    calls: list[int] = []

    w = watcher(tmp_path, lambda: (calls.append(1), fired.set()))
    w.start()
    write_shutdown_request(tmp_path, source="upgrade")

    assert fired.wait(timeout=3), "the watcher never saw the sentinel"
    assert _wait_for(lambda: not shutdown_request_path(tmp_path).exists())
    # Single-shot: the thread is done, so a second request goes unheard by it.
    assert _wait_for(lambda: w._thread is None or not w._thread.is_alive())
    assert calls == [1]


def test_the_file_is_consumed_before_the_callback_runs(tmp_path, watcher) -> None:
    """A crash inside the callback must not leave a sentinel that stops the
    next server the instant it boots."""
    seen: list[bool] = []

    def _on_request() -> None:
        seen.append(shutdown_request_path(tmp_path).exists())

    w = watcher(tmp_path, _on_request)
    w.start()
    write_shutdown_request(tmp_path, source="upgrade")

    assert _wait_for(lambda: bool(seen))
    assert seen == [False]


def test_a_raising_callback_does_not_take_the_thread_down_noisily(
    tmp_path, watcher
) -> None:
    fired = threading.Event()

    def _boom() -> None:
        fired.set()
        raise RuntimeError("the loop was already gone")

    w = watcher(tmp_path, _boom)
    w.start()
    write_shutdown_request(tmp_path, source="restore")

    assert fired.wait(timeout=3)
    assert _wait_for(lambda: w._thread is None or not w._thread.is_alive())


def test_a_cleared_sentinel_is_never_acted_on(tmp_path, watcher) -> None:
    """The boot-time clear is what makes a leftover from a crashed restart
    harmless."""
    calls: list[int] = []

    write_shutdown_request(tmp_path, source="upgrade")
    clear_stale_shutdown_request(tmp_path)

    w = watcher(tmp_path, lambda: calls.append(1))
    w.start()
    time.sleep(_POLL_S * 6)

    assert calls == []


def test_stopping_the_watcher_ends_the_watch(tmp_path, watcher) -> None:
    calls: list[int] = []

    w = watcher(tmp_path, lambda: calls.append(1))
    w.start()
    w.stop()
    write_shutdown_request(tmp_path, source="upgrade")
    time.sleep(_POLL_S * 6)

    assert calls == []
    assert shutdown_request_path(tmp_path).is_file(), "nothing consumed it"


def test_stop_is_safe_without_a_start(tmp_path) -> None:
    ShutdownRequestWatcher(tmp_path, lambda: None).stop()  # must not raise


def test_the_default_poll_interval_is_sane() -> None:
    """It runs for the life of the server, and the runners' grace budget has
    to absorb a full interval before the shutdown even begins."""
    assert 0 < shutdown_watch.DEFAULT_POLL_INTERVAL_S <= 2.0
