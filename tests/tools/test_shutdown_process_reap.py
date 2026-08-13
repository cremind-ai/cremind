"""Managed subprocesses must not outlive the server.

Nothing persists a spawned process's pid, so a child that survives shutdown is
invisible to the next boot — and a skill listener orphaned that way keeps
holding ``scripts/.listener.lock``, which makes every autostart attempt on the
next boot exit "another listener is already running".

The reap also has to stay inside the shutdown budget: ``_do_shutdown`` is
wrapped in ``asyncio.wait_for(SHUTDOWN_TIMEOUT_S)``, and a container restart
depends on that bound holding.

Tests use ``asyncio.run`` per the repo idiom (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from app.tools.builtin import exec_shell
from app.tools.builtin.exec_shell import (
    ProcessInfo,
    _process_registry,
    stop_all_managed_processes,
)


class _FakeProc:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15


def _register(process_id: str, pid: int, **over) -> _FakeProc:
    proc = _FakeProc(pid)
    _process_registry[process_id] = ProcessInfo(
        process=proc,
        created_at=time.time(),
        working_dir=over.get("working_dir", "/tmp/x"),
        command=over.get("command", "uv run scripts/event_listener.py"),
        log_dir="",
        log_writer_state=None,
        expire_time=float("inf"),
        is_pty=False,
        task_id=None,
        profile=over.get("profile", "admin"),
        autostart_id=over.get("autostart_id", "row-1"),
    )
    return proc


def _clear_registry():
    _process_registry.clear()


def test_reap_kills_every_registered_process_tree():
    _clear_registry()
    killed: list = []
    a = _register("p1", 100)
    b = _register("p2", 200, profile="other", autostart_id=None)

    with patch.object(exec_shell, "_kill_process_tree",
                      lambda pid, timeout=15.0: killed.append(pid)):
        count = asyncio.run(stop_all_managed_processes())

    assert count == 2
    # The tree kill is the part that matters: the tracked leader is a shell
    # wrapper, and it's the `uv run -> python` grandchild that holds the lock.
    assert sorted(killed) == [100, 200]
    assert a.terminated and b.terminated
    assert _process_registry == {}, "registry must be emptied"


def test_reap_spans_all_profiles():
    """Unlike stop_processes_for_profile, shutdown is not profile-scoped — the
    whole process is going away."""
    _clear_registry()
    _register("p1", 1, profile="admin")
    _register("p2", 2, profile="tenant-b")
    _register("p3", 3, profile=None)

    with patch.object(exec_shell, "_kill_process_tree", lambda pid, timeout=15.0: None):
        assert asyncio.run(stop_all_managed_processes()) == 3
    assert _process_registry == {}


def test_reap_is_a_noop_with_nothing_registered():
    _clear_registry()
    assert asyncio.run(stop_all_managed_processes()) == 0


def test_one_failing_kill_does_not_block_the_others():
    _clear_registry()
    _register("p1", 1)
    good = _register("p2", 2)

    def _kill(pid, timeout=15.0):
        if pid == 1:
            raise OSError("access denied")

    with patch.object(exec_shell, "_kill_process_tree", _kill):
        count = asyncio.run(stop_all_managed_processes())

    assert count == 2
    assert good.terminated, "a failure on one process must not strand the rest"
    assert _process_registry == {}


def test_already_exited_process_is_not_signalled_again():
    _clear_registry()
    proc = _register("p1", 1)
    proc.returncode = 0  # exited on its own before shutdown
    killed: list = []

    with patch.object(exec_shell, "_kill_process_tree",
                      lambda pid, timeout=15.0: killed.append(pid)):
        asyncio.run(stop_all_managed_processes())

    assert killed == []
    assert not proc.terminated
    assert _process_registry == {}


def test_shutdown_stays_bounded_when_a_kill_hangs():
    """A wedged taskkill must not eat the whole shutdown budget.

    The reap carries its own sub-bound (3s) so the remaining cleanup steps still
    get to run and a container restart isn't stalled. Asserting against that
    inner bound — not just SHUTDOWN_TIMEOUT_S — is what proves the sub-bound is
    wired, since the outer wait_for would mask its absence.
    """
    from app import server

    _clear_registry()
    _register("p1", 1)

    hang_s = 8.0  # > the reap's own bound, < SHUTDOWN_TIMEOUT_S

    def _slow_kill(pid, timeout=15.0):
        time.sleep(hang_s)

    async def _run() -> float:
        with patch.object(exec_shell, "_kill_process_tree", _slow_kill):
            start = time.monotonic()
            await server._on_shutdown()
            return time.monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed < hang_s - 1.0, (
        f"reap should have been cut short by its own bound, took {elapsed:.1f}s"
    )
    assert elapsed < server.SHUTDOWN_TIMEOUT_S + 2.0
    _clear_registry()
