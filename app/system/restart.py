"""Detached watchdog for the in-app restart.

Invoked as a sibling subprocess of the running HTTP server:

    python -m app.system.restart --parent-pid <PID> [--grace <seconds>]

Stopping the server is no longer this process's job. ``POST
/api/system/restart`` asks the server to stop *itself*
(:func:`app.server.request_graceful_shutdown`), which runs the same shutdown
an OS signal would: connections drain, channel adapters stop their node
sidecars, managed processes are tree-killed, lock files are released, and only
then does the process exit. That matters most on Windows, where there is no
other way to get it — ``os.kill(pid, SIGTERM)`` maps onto ``TerminateProcess``,
which skips the lifespan hook entirely and orphans every child the server had.

This process exists for the one case an in-process shutdown cannot cover: a
wedged event loop, where the request is never processed and no in-process
deadline ever fires. So it waits up to ``--grace`` seconds for the parent to
exit on its own, and hard-kills it only if it is still alive at the deadline.
A separate process is needed because the server cannot wait for its own death
in a request handler — the response has to reach the client first.

What happens after the exit is supervisor-dependent:

- Under Docker: ``restart: unless-stopped`` brings the container back.
- Under Electron: the main process's IPC restart handler is the
  preferred path (kills + respawns in-process); if the renderer falls
  back to this HTTP path, ``backendProcess.on('exit')`` fires but
  the main process does not auto-respawn — the user would see a
  dead backend.
- Under the boot service ``cremind boot enable`` registers (a systemd unit,
  a LaunchAgent, or a Windows respawn loop): the service restarts the
  backend within a couple of seconds. This is what makes the restart — and
  the ``CREMIND_SSL=after-setup`` switch to HTTPS, which *is* a restart —
  work on a native install.
- Under a bare, unsupervised ``cremind serve``: the backend exits and stays
  down.

The UI's confirmation dialog warns about these consequences before
the user gets here.

Stdlib-only and free of ``app.config`` imports: ``app.upgrade.detached`` and
``app.backup.detached`` reuse :func:`wait_for_parent_exit` for the same
wait-then-escalate handshake.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


# How long a deliberate shutdown may take before the watchdog stops being
# patient. The worst realistic chain is 1.5s (response window) + 10s
# (connection drain) + 8s (SHUTDOWN_TIMEOUT_S lifespan cleanup) ~= 19.5s
# unsupervised; where a supervisor exists ``_BoundedShutdownServer``'s own
# 12s ``os._exit`` timer caps it well below that. The timer runs concurrently
# with the drain, never after it, so 25s clears both readings with headroom.
# Anything past this deadline is not slow, it is stuck.
DEFAULT_GRACE_S = 25.0

# Poll intervals for the fallback liveness checks. The Windows primary path
# blocks on a kernel handle instead and needs neither.
_POSIX_POLL_S = 0.25
_WINDOWS_POLL_S = 0.5

# Win32 constants (winnt.h / winbase.h).
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_WAIT_OBJECT_0 = 0x00000000
_ERROR_ACCESS_DENIED = 5
# WaitForSingleObject takes a DWORD of milliseconds; 0xFFFFFFFF is INFINITE,
# which is never what we want from a deadline.
_MAX_WAIT_MS = 0xFFFFFFFE


def _ms(seconds: float) -> int:
    """``seconds`` as the DWORD millisecond count Win32 waits take."""
    if seconds <= 0:
        return 0
    return min(int(seconds * 1000), _MAX_WAIT_MS)


def _kernel32():
    """``kernel32`` with the signatures this module uses, or None.

    Argument and return types are declared explicitly: ctypes defaults a
    return to ``c_int``, which truncates a 64-bit HANDLE into something that
    cannot be waited on or closed.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32
    except Exception:  # noqa: BLE001 - any ctypes failure falls back to polling
        return None


def _process_alive_tasklist(pid: int) -> bool:
    """Windows liveness via ``tasklist``, for when ctypes cannot answer.

    A tool that fails to run counts as "still alive": the watchdog's job is to
    guarantee an exit, so an unknown must keep it waiting rather than let it
    declare success. Only a properly quoted CSV row is a match — tasklist's
    "no tasks" line is localized (mirrors ``boot_service._process_name``).
    """
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if proc.returncode != 0:
        return True
    return (proc.stdout or "").strip().startswith('"')


def _wait_tasklist(pid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if not _process_alive_tasklist(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_WINDOWS_POLL_S)


def _wait_windows(pid: int, timeout_s: float) -> bool:
    """Block on the process handle until it exits or ``timeout_s`` elapses."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return _wait_tasklist(pid, timeout_s)
    import ctypes

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        if ctypes.get_last_error() == _ERROR_ACCESS_DENIED:
            return _wait_tasklist(pid, timeout_s)
        # ERROR_INVALID_PARAMETER: no process carries that id any more.
        return True
    try:
        return kernel32.WaitForSingleObject(handle, _ms(timeout_s)) == _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def _reap_if_child(pid: int) -> bool:
    """Reap ``pid`` if it happens to be our own child. True once it is gone.

    Normally it is our *parent*, which raises ``ChildProcessError`` and leaves
    the answer to the probes below. It is our child when a caller supervises
    something it spawned, and then only a wait clears the zombie.
    """
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return reaped == pid


def _is_zombie(pid: int) -> bool:
    """Whether ``pid`` has exited but nobody has reaped it yet (Linux).

    A zombie answers ``os.kill(pid, 0)`` exactly as a live process does — the
    table entry survives until its parent waits on it — so a poll trusting the
    signal alone sits out the entire grace against a process that is already
    dead, and then "escalates" to killing a corpse. ``/proc`` is Linux-only;
    everywhere else this is False and the bounded wait stands as the answer.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    # "<pid> (comm) S ..." — comm can contain spaces and parentheses, so the
    # state is the first field after the LAST ')'.
    marker = data.rfind(b")")
    if marker == -1:
        return False
    return data[marker + 2:marker + 3] == b"Z"


def _wait_posix(pid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _reap_if_child(pid):
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Alive, owned by another user. Not a supported configuration, but
            # it is emphatically not "exited".
            pass
        else:
            if _is_zombie(pid):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POSIX_POLL_S)


def wait_for_parent_exit(pid: int, timeout_s: float) -> bool:
    """Wait up to ``timeout_s`` for ``pid`` to exit. True if it did.

    NEVER probes with ``os.kill(pid, 0)`` on Windows: there, ``os.kill``
    ignores the signal number and terminates the target outright, so the
    "harmless liveness check" of POSIX is a kill.
    """
    if pid <= 0:
        return True
    if sys.platform == "win32":
        return _wait_windows(pid, timeout_s)
    return _wait_posix(pid, timeout_s)


def hard_kill(pid: int) -> None:
    """Stop ``pid`` unconditionally. Best-effort; never raises."""
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            # Windows has no SIGKILL. ``os.kill`` with any signal other than
            # CTRL_C_EVENT/CTRL_BREAK_EVENT calls TerminateProcess, which is
            # exactly the unconditional stop wanted here.
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        # Exited between the wait timing out and the kill landing.
        pass
    except PermissionError:
        # Only reachable if the API runs as a different user than the backend;
        # not a supported configuration.
        pass


def _watch_windows(pid: int, grace_s: float) -> None:
    """Wait-then-terminate through a single handle opened up front.

    Windows recycles PIDs aggressively. Opening the handle while the parent is
    provably alive pins the identity for the whole wait, so the escalation can
    never land on whatever process inherited the number in the meantime.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        if not _wait_tasklist(pid, grace_s):
            hard_kill(pid)
        return
    import ctypes

    handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_TERMINATE, False, pid)
    if not handle:
        if ctypes.get_last_error() != _ERROR_ACCESS_DENIED:
            # Already gone — the shutdown beat us here, which is the good case.
            return
        # Denied the TERMINATE right; fall back to the pid-addressed path.
        if not _wait_tasklist(pid, grace_s):
            hard_kill(pid)
        return
    try:
        if kernel32.WaitForSingleObject(handle, _ms(grace_s)) == _WAIT_OBJECT_0:
            return
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cremind-system-restart")
    parser.add_argument(
        "--parent-pid",
        type=int,
        required=True,
        help="PID of the HTTP server whose exit this process guarantees.",
    )
    parser.add_argument(
        "--grace",
        type=float,
        default=DEFAULT_GRACE_S,
        help=(
            "Seconds to let the server shut itself down before it is killed "
            f"(default: {DEFAULT_GRACE_S})."
        ),
    )
    args = parser.parse_args(argv)

    pid = args.parent_pid
    if pid <= 0:
        return 0
    if sys.platform == "win32":
        _watch_windows(pid, args.grace)
    elif not wait_for_parent_exit(pid, args.grace):
        hard_kill(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
