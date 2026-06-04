"""PTY (pseudo-terminal) support for the Exec Shell tool.

Provides a :class:`PtyProcess` adapter that duck-types the subset of
``asyncio.subprocess.Process`` that :mod:`app.tools.builtin.exec_shell`
consumes, so the existing classifier / log-writer / input / stop paths
work for PTY-backed processes with no branching.

Unix backend uses stdlib ``pty``; Windows backend uses ``pywinpty``
(ConPTY) and is imported lazily so Unix installs don't need it.

A PTY is a single device — stdout and stderr are merged on the tty, so
the adapter exposes an immediately-EOF'd ``.stderr`` reader. The log
writer's multi-stream loop sees EOF on stderr in the first cycle and
operates single-stream from there.
"""

from __future__ import annotations

import asyncio
import os
import platform
import queue
import re
import threading
from typing import Optional

from app.tools.builtin.exec_shell_rtk import maybe_rewrite_command
from app.utils.logger import logger

_SYSTEM = platform.system()


def _pty_shell_for(system: str) -> tuple[str, list[str]]:
    """Return (shell, flags) pair for PTY spawning on the given OS."""
    if system == "Windows":
        return "powershell.exe", ["-NoLogo", "-NoProfile", "-Command"]
    return "/bin/bash", ["-c"]


# ---------------------------------------------------------------------------
# Stdin writer shared by both backends (matches asyncio.StreamWriter surface
# used in exec_shell.py: .write(bytes) + async .drain()).
# ---------------------------------------------------------------------------

class _PtyStdinWriter:
    def __init__(self, write_fn, drain_fn=None):
        self._write = write_fn
        self._drain = drain_fn

    def write(self, data) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        self._write(data)

    async def drain(self) -> None:
        if self._drain is not None:
            await self._drain()

    def close(self) -> None:
        # Closing stdin on a PTY is effectively sending EOF; the adapter's
        # close_master()/send_eof() handles that explicitly.
        return


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class PtyProcess:
    """Duck-typed subset of asyncio.subprocess.Process + PTY extras."""
    pid: int
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    stdin: _PtyStdinWriter

    @property
    def returncode(self) -> Optional[int]:
        raise NotImplementedError

    async def wait(self) -> int:
        raise NotImplementedError

    def terminate(self) -> None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    def send_eof(self) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def close_master(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Unix implementation: stdlib pty + asyncio loop.add_reader on master fd
# ---------------------------------------------------------------------------

class _UnixPtyProcess(PtyProcess):
    def __init__(self, master_fd: int, proc: asyncio.subprocess.Process):
        self._master_fd = master_fd
        self._proc = proc
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._reader_registered = False

        self.stdout = asyncio.StreamReader(limit=2**20)
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = _PtyStdinWriter(self._write_master)

        self._loop.add_reader(self._master_fd, self._on_readable)
        self._reader_registered = True

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            # Linux raises EIO when all slave writers close; treat as EOF.
            self._feed_eof_and_unregister()
            return
        if not data:
            self._feed_eof_and_unregister()
            return
        self.stdout.feed_data(data)

    def _feed_eof_and_unregister(self) -> None:
        try:
            self.stdout.feed_eof()
        except Exception:
            pass
        if self._reader_registered:
            try:
                self._loop.remove_reader(self._master_fd)
            except Exception:
                pass
            self._reader_registered = False

    def _write_master(self, data: bytes) -> None:
        # master fd is non-blocking; loop for partial writes.
        total = 0
        while total < len(data):
            try:
                n = os.write(self._master_fd, data[total:])
            except BlockingIOError:
                # Write buffer full; drop rather than block the event loop.
                break
            except OSError:
                break
            if n <= 0:
                break
            total += n

    async def wait(self) -> int:
        return await self._proc.wait()

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass

    def kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def send_eof(self) -> None:
        try:
            os.write(self._master_fd, b"\x04")
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            import fcntl
            import struct
            import termios
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def close_master(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._feed_eof_and_unregister()
        try:
            os.close(self._master_fd)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows implementation: pywinpty via dedicated reader/writer threads
# ---------------------------------------------------------------------------

class _WindowsPtyProcess(PtyProcess):
    def __init__(self, pty_proc, loop: asyncio.AbstractEventLoop):
        self._pty = pty_proc
        self._loop = loop
        self._closed = False
        self._returncode: Optional[int] = None

        self.stdout = asyncio.StreamReader(limit=2**20)
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

        self._write_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.stdin = _PtyStdinWriter(self._enqueue_write, self._drain)

        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="pty-reader", daemon=True,
        )
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="pty-writer", daemon=True,
        )
        self._reader_thread.start()
        self._writer_thread.start()

    @property
    def pid(self) -> int:
        try:
            return int(self._pty.pid)
        except Exception:
            return -1

    @property
    def returncode(self) -> Optional[int]:
        if self._returncode is not None:
            return self._returncode
        try:
            if not self._pty.isalive():
                self._returncode = self._resolve_exit_status()
        except Exception:
            pass
        return self._returncode

    def _resolve_exit_status(self) -> int:
        try:
            rc = getattr(self._pty, "exitstatus", None)
            if rc is None:
                rc = -1
            return int(rc)
        except Exception:
            return -1

    def _reader_loop(self) -> None:
        while not self._closed:
            try:
                chunk = self._pty.read(4096)
            except EOFError:
                break
            except Exception as exc:
                logger.debug(f"pty reader_loop ended: {exc}")
                break
            if not chunk:
                break
            if isinstance(chunk, str):
                data = chunk.encode("utf-8", errors="replace")
            else:
                data = bytes(chunk)
            self._loop.call_soon_threadsafe(self.stdout.feed_data, data)
        self._loop.call_soon_threadsafe(self._safe_feed_eof)

    def _safe_feed_eof(self) -> None:
        try:
            self.stdout.feed_eof()
        except Exception:
            pass

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None or self._closed:
                break
            try:
                payload = item.decode("utf-8", errors="replace")
                self._pty.write(payload)
            except Exception as exc:
                logger.debug(f"pty writer_loop write failed: {exc}")
                # keep draining the queue so drain() can complete
                continue

    def _enqueue_write(self, data: bytes) -> None:
        if self._closed:
            return
        self._write_queue.put(data)

    async def _drain(self) -> None:
        # Best-effort: wait for the queue to empty.
        while not self._closed and not self._write_queue.empty():
            await asyncio.sleep(0.01)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.1)
        return self._returncode  # type: ignore[return-value]

    def terminate(self) -> None:
        try:
            self._pty.terminate(force=False)
        except Exception:
            pass

    def kill(self) -> None:
        try:
            self._pty.terminate(force=True)
        except Exception:
            pass

    def send_eof(self) -> None:
        # ConPTY has no clean EOF; Ctrl+Z/Ctrl+D works for most consumers.
        try:
            self._enqueue_write(b"\x04")
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._pty.setwinsize(rows, cols)
        except Exception:
            pass

    def close_master(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._write_queue.put_nowait(None)
        except Exception:
            pass
        try:
            self._pty.close(force=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Spawn helpers
# ---------------------------------------------------------------------------

async def _spawn_unix_pty(
    command: str, working_dir: str, cols: int, rows: int, system: str,
    extra_env: Optional[dict[str, str]] = None,
) -> PtyProcess:
    import fcntl
    import pty
    import struct
    import termios

    shell, flags = _pty_shell_for(system)
    master_fd, slave_fd = pty.openpty()
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        env["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            env.update(extra_env)

        proc = await asyncio.create_subprocess_exec(
            shell, *flags, command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=working_dir,
            env=env,
            start_new_session=True,
        )
    finally:
        try:
            os.close(slave_fd)
        except Exception:
            pass

    return _UnixPtyProcess(master_fd, proc)


async def _spawn_windows_pty(
    command: str, working_dir: str, cols: int, rows: int, system: str,
    extra_env: Optional[dict[str, str]] = None,
) -> PtyProcess:
    try:
        import winpty  # type: ignore  # PyPI package: `pywinpty`; import name: `winpty`
    except ImportError as exc:
        raise RuntimeError(
            "pywinpty is required for PTY mode on Windows. "
            "Install it with `pip install pywinpty`."
        ) from exc

    shell, flags = _pty_shell_for(system)
    loop = asyncio.get_running_loop()
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = str(cols)
    env["LINES"] = str(rows)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    argv = [shell, *flags, command]

    def _spawn():
        return winpty.PtyProcess.spawn(
            argv, cwd=working_dir, env=env, dimensions=(rows, cols),
        )

    pty_proc = await loop.run_in_executor(None, _spawn)
    return _WindowsPtyProcess(pty_proc, loop)


async def _spawn_command_pty(
    command: str, working_dir: str, cols: int = 80, rows: int = 24,
    system: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    rtk_enabled: bool = False, rtk_binary: str = "rtk",
) -> PtyProcess:
    system = system or _SYSTEM
    command = await maybe_rewrite_command(
        command, enabled=rtk_enabled, binary=rtk_binary,
    )
    if system == "Windows":
        return await _spawn_windows_pty(command, working_dir, cols, rows, system, extra_env=extra_env)
    return await _spawn_unix_pty(command, working_dir, cols, rows, system, extra_env=extra_env)


# ---------------------------------------------------------------------------
# Runtime detection: did a non-PTY execution fail because a TTY was required?
#
# Rather than hardcode a list of "commands that need a TTY", we observe the
# actual runtime output. Programs that need a controlling terminal emit a
# recognizable error and exit. When that error signal is present we can
# respawn the same command under a PTY and get the real behavior.
#
# Note: this cannot help commands that *hang* without a TTY (e.g. `ssh`
# waiting on a password). Those still require the caller to pass
# `pty: true` explicitly.
# ---------------------------------------------------------------------------

_TTY_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\binput device is not a (?:tty|terminal)\b",
        r"\bnot a (?:tty|terminal)\b",
        r"\bno (?:tty|terminal) (?:present|available)\b",
        r"\bstdin is not a (?:tty|terminal)\b",
        r"\binappropriate ioctl for device\b",
        r"\bmust be run (?:interactively|from a terminal|in a terminal)\b",
        r"\brequires? a (?:controlling )?(?:tty|terminal|pseudo[- ]?terminal)\b",
        r"\bpseudo[- ]?terminal will not be allocated\b",
        r"\bunable to (?:open|allocate) (?:a )?(?:tty|pty|terminal)\b",
        r"\bthe handle is invalid\b.*\b(?:console|terminal)\b",
    )
]


def _looks_like_tty_error(*texts: str) -> bool:
    """Scan combined output for phrases programs emit when a TTY is missing."""
    combined = "\n".join(t for t in texts if t)
    if not combined:
        return False
    return any(pat.search(combined) for pat in _TTY_ERROR_PATTERNS)
