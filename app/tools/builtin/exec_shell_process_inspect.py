"""Cross-platform child-process introspection for the Exec Shell tool.

Determines whether a child process of the persistent shell session is
blocked on a ``read()`` call (waiting for stdin input) versus actively
running or waiting on network / timer events.

Platform support
----------------
- **Linux**: reads ``/proc/<pid>/wchan`` and ``/proc/<pid>/stat`` directly.
- **macOS (Darwin)**: uses ``pgrep -P`` and ``ps -o wchan=`` subprocesses.
- **Windows**: returns ``None`` (inconclusive).  The Exec Shell tool falls
  back to heuristics and the pre-execution classifier on Windows.
"""

from __future__ import annotations

import asyncio
import os
from typing import Sequence

from app.utils.logger import logger


# ---------------------------------------------------------------------------
# wchan values that indicate "blocked on stdin / pipe read"
# ---------------------------------------------------------------------------

_STDIN_BLOCKED_WCHANS: frozenset[str] = frozenset({
    # Linux
    "pipe_read", "pipe_wait", "wait_woken",
    "n_tty_read", "tty_read", "unix_stream_recvmsg",
    # macOS
    "pipe_r", "piperd", "read", "ttyin", "ttyrd",
})

# wchan values that indicate "running a daemon / server / I/O loop"
_DAEMON_WCHANS: frozenset[str] = frozenset({
    # Linux
    "ep_poll", "do_epoll_wait", "do_select", "do_poll",
    "poll_schedule_timeout", "inet_csk_accept",
    "futex_wait_queue", "futex_wait",
    "sk_wait_data", "sock_recvmsg",
    # macOS
    "select", "kqread", "accept", "kevent",
})


# ---------------------------------------------------------------------------
# Linux implementation
# ---------------------------------------------------------------------------

def _find_children_linux(parent_pid: int) -> list[int]:
    """Find direct child PIDs by scanning ``/proc``."""
    children: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return children

    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "r") as f:
                stat_line = f.read()
            # Format: "<pid> (<comm>) <state> <ppid> ..."
            # The comm field can contain spaces and parens, so split from
            # the *last* closing paren.
            after_comm = stat_line.rsplit(")", 1)
            if len(after_comm) < 2:
                continue
            fields = after_comm[1].split()
            # fields[0] = state, fields[1] = ppid
            ppid = int(fields[1])
            if ppid == parent_pid:
                children.append(int(entry))
        except (FileNotFoundError, PermissionError, ValueError, IndexError, OSError):
            continue
    return children


def _get_deepest_child_linux(root_pid: int, max_depth: int = 10) -> int:
    """Walk the process tree down to the deepest single child.

    If a process has multiple children, stop and return that process
    (it's likely a shell managing a pipeline).
    """
    current = root_pid
    for _ in range(max_depth):
        children = _find_children_linux(current)
        if len(children) == 1:
            current = children[0]
        else:
            break
    return current


def _read_wchan_linux(pid: int) -> str | None:
    """Read ``/proc/<pid>/wchan``.  Returns ``None`` on failure."""
    try:
        with open(f"/proc/{pid}/wchan", "r") as f:
            wchan = f.read().strip()
        return wchan if wchan and wchan != "0" else None
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _is_blocked_on_read_linux(shell_pid: int) -> bool | None:
    """Linux: check if the shell's child command is blocked on stdin read."""
    target = _get_deepest_child_linux(shell_pid)
    if target == shell_pid:
        # No child found — the shell itself is the process.
        # Check the shell's own wchan.
        pass

    wchan = _read_wchan_linux(target)
    if wchan is None:
        return None  # Inconclusive

    if wchan in _STDIN_BLOCKED_WCHANS:
        return True
    if wchan in _DAEMON_WCHANS:
        return False
    # Unknown wchan — inconclusive.
    logger.debug(f"exec_shell_inspect: unknown wchan '{wchan}' for pid {target}")
    return None


# ---------------------------------------------------------------------------
# macOS (Darwin) implementation
# ---------------------------------------------------------------------------

async def _find_children_darwin(parent_pid: int) -> list[int]:
    """Find child PIDs using ``pgrep -P``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-P", str(parent_pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return [
            int(line)
            for line in stdout.decode().strip().split("\n")
            if line.strip().isdigit()
        ]
    except (asyncio.TimeoutError, FileNotFoundError, OSError, ValueError):
        return []


async def _get_deepest_child_darwin(root_pid: int, max_depth: int = 10) -> int:
    """Walk the process tree down to the deepest single child (macOS)."""
    current = root_pid
    for _ in range(max_depth):
        children = await _find_children_darwin(current)
        if len(children) == 1:
            current = children[0]
        else:
            break
    return current


async def _read_wchan_darwin(pid: int) -> str | None:
    """Read the wait channel via ``ps -o wchan=``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-o", "wchan=", "-p", str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        wchan = stdout.decode().strip()
        return wchan if wchan else None
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return None


async def _is_blocked_on_read_darwin(shell_pid: int) -> bool | None:
    """macOS: check if the shell's child command is blocked on stdin read."""
    target = await _get_deepest_child_darwin(shell_pid)

    wchan = await _read_wchan_darwin(target)
    if wchan is None:
        return None

    wchan_lower = wchan.lower().strip()
    if wchan_lower in _STDIN_BLOCKED_WCHANS:
        return True
    if wchan_lower in _DAEMON_WCHANS:
        return False

    logger.debug(f"exec_shell_inspect: unknown wchan '{wchan}' for pid {target}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def is_child_blocked_on_read(shell_pid: int, system: str) -> bool | None:
    """Determine if the child process of *shell_pid* is blocked on stdin.

    Parameters
    ----------
    shell_pid:
        PID of the persistent shell process (bash / PowerShell).
    system:
        ``platform.system()`` value: ``"Linux"``, ``"Darwin"``, ``"Windows"``.

    Returns
    -------
    bool | None
        ``True``  — child is blocked on stdin read (waiting for input).
        ``False`` — child is *not* blocked on stdin (likely a daemon / server).
        ``None``  — could not determine (platform unsupported or inspection failed).
    """
    try:
        if system == "Linux":
            return _is_blocked_on_read_linux(shell_pid)
        elif system == "Darwin":
            return await _is_blocked_on_read_darwin(shell_pid)
        else:
            # Windows: no reliable introspection without psutil.
            return None
    except Exception as exc:
        logger.debug(f"exec_shell_inspect: introspection failed for pid {shell_pid}: {exc}")
        return None
