"""Tests for the detached restart watchdog (app/system/restart.py).

The watchdog's whole contract is "the process WILL be gone": wait for the
server's own graceful shutdown, escalate only if it wedges. Both halves are
easy to get wrong in ways no unit test with a fake pid would catch — the
Windows path blocks on a real kernel handle, and ``os.kill(pid, 0)``, the
POSIX liveness idiom, *terminates* the target on Windows. So these tests
drive real child processes.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from app.system import restart


def _sleeper(seconds: float) -> subprocess.Popen:
    """A child that does nothing for ``seconds``, then exits 0."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def sleeper():
    """A long-lived child, reliably reaped however the test ends."""
    procs: list[subprocess.Popen] = []

    def _make(seconds: float = 30.0) -> subprocess.Popen:
        proc = _sleeper(seconds)
        procs.append(proc)
        return proc

    yield _make
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


# ── waiting ──────────────────────────────────────────────────────────────


def test_a_process_that_exits_is_waited_out_not_timed_out(sleeper) -> None:
    """Also pins the POSIX zombie hazard: nothing here reaps the child, and an
    unreaped process answers ``os.kill(pid, 0)`` exactly as a live one does —
    so a naive poll would sit out the full budget against a corpse and then
    "escalate" to killing it.
    """
    proc = sleeper(0.5)
    started = time.monotonic()

    assert restart.wait_for_parent_exit(proc.pid, 10.0) is True
    # It returned because the child exited, not because it slept the budget.
    assert time.monotonic() - started < 5.0
    proc.wait(timeout=5)


def test_a_process_that_stays_up_times_out(sleeper) -> None:
    proc = sleeper(30.0)

    assert restart.wait_for_parent_exit(proc.pid, 0.5) is False
    assert proc.poll() is None, "the wait must not kill anything on its own"


def test_an_already_reaped_process_reads_as_exited(sleeper) -> None:
    proc = sleeper(0.05)
    proc.wait(timeout=10)

    assert restart.wait_for_parent_exit(proc.pid, 5.0) is True


def test_pid_zero_is_never_waited_on() -> None:
    """0 means "nothing to supervise" — on POSIX it would signal the whole
    process group, which is emphatically not the ask."""
    started = time.monotonic()
    assert restart.wait_for_parent_exit(0, 30.0) is True
    assert time.monotonic() - started < 1.0


# ── escalation ───────────────────────────────────────────────────────────


def test_hard_kill_stops_a_wedged_process(sleeper) -> None:
    proc = sleeper(30.0)

    restart.hard_kill(proc.pid)

    assert proc.wait(timeout=10) is not None
    assert proc.poll() is not None


def test_hard_kill_of_a_dead_process_is_silent(sleeper) -> None:
    proc = sleeper(0.05)
    proc.wait(timeout=10)

    restart.hard_kill(proc.pid)  # must not raise


# ── main() ───────────────────────────────────────────────────────────────


def test_main_kills_a_parent_that_outlives_its_grace(sleeper) -> None:
    proc = sleeper(30.0)

    assert restart.main(["--parent-pid", str(proc.pid), "--grace", "1"]) == 0

    assert proc.wait(timeout=10) is not None


def test_main_leaves_a_parent_that_stops_on_its_own(sleeper) -> None:
    """The normal case: the server shut itself down and the watchdog is a
    no-op that simply observes it."""
    proc = sleeper(0.5)

    assert restart.main(["--parent-pid", str(proc.pid), "--grace", "20"]) == 0

    # Exit code 0 = its own clean exit, not a kill.
    assert proc.wait(timeout=5) == 0


def test_main_with_pid_zero_does_nothing() -> None:
    started = time.monotonic()
    assert restart.main(["--parent-pid", "0", "--grace", "30"]) == 0
    assert time.monotonic() - started < 1.0


def test_grace_defaults_to_the_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint passes --grace explicitly, but a hand-run watchdog must
    not fall back to something arbitrary."""
    seen: list[float] = []

    # Whichever branch this platform takes, it gets the same budget.
    monkeypatch.setattr(
        restart, "wait_for_parent_exit", lambda pid, timeout_s: seen.append(timeout_s) or True
    )
    monkeypatch.setattr(
        restart, "_watch_windows", lambda pid, grace_s: seen.append(grace_s)
    )

    assert restart.main(["--parent-pid", "4242"]) == 0
    assert seen == [restart.DEFAULT_GRACE_S]
