"""The installer TUI reading a real terminal the way the shells start it.

install.sh runs the TUI as ``... </dev/tty >/dev/tty`` — under ``curl |
bash`` the script itself is on stdin. macOS's kqueue, which asyncio's default
event loop polls with there, refuses a ``/dev/tty`` descriptor with EINVAL, so
on a Mac the first screen died with EOFError before it drew and the shell
reported "Installer cancelled.". :func:`app.installer.tui._run` runs every
dialog on a select()-based loop on macOS instead.

The terminal test drives a real dialog in a child process on a pseudo-
terminal, its stdin and stdout reopened from ``/dev/tty`` as the shell does.
Off macOS the child swaps in a default selector that refuses terminal
descriptors the way kqueue does and reports itself as macOS, so Linux CI takes
the path a Mac takes; on a Mac the refusal is the real one.
"""

from __future__ import annotations

import asyncio
import os
import select
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.installer import tui


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_run_uses_a_select_loop_on_macos(monkeypatch) -> None:
    """Every dialog runs through ``_run``: a select() loop on macOS, plain
    ``app.run()`` everywhere else."""

    class FakeApp:
        def run(self) -> str:
            return "app.run()"

        async def run_async(self) -> type:
            return type(asyncio.get_running_loop()._selector)

    monkeypatch.setattr(sys, "platform", "darwin")
    assert tui._run(FakeApp()) is selectors.SelectSelector
    for platform in ("linux", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        assert tui._run(FakeApp()) == "app.run()"


# Runs in the child: one real dialog, reading the terminal the shell's way.
_CHILD = r'''
import errno, fcntl, os, selectors, sys, termios

# Take the pty as this session's controlling terminal, then reopen /dev/tty
# over stdin and stdout: what `</dev/tty >/dev/tty` hands the TUI.
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
tty = os.open("/dev/tty", os.O_RDWR)
os.dup2(tty, 0)
os.dup2(tty, 1)

from app.installer import tui

if sys.platform != "darwin":
    # Stand in for macOS, whose kqueue refuses a /dev/tty descriptor. Only
    # after the imports: urllib, for one, picks its macOS code at import.
    class KqueueLike(selectors.DefaultSelector):
        def register(self, fileobj, events, data=None):
            fd = fileobj if isinstance(fileobj, int) else fileobj.fileno()
            if os.isatty(fd):
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
            return super().register(fileobj, events, data)

    selectors.DefaultSelector = KqueueLike
    sys.platform = "darwin"

answer = tui._radio(
    "Terminal test", "Pick one", [("a", "Apple"), ("b", "Banana")],
    default="b", allow_back=False,
)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(repr(answer))
'''


def _read_terminal(fd: int, until: bytes | None, timeout: float = 30.0) -> bytes:
    """What the child writes to the terminal, up to ``until`` — or, with
    ``None``, until it closes it. Fails with the screen so far otherwise."""
    seen = b""
    deadline = time.monotonic() + timeout
    while until is None or until not in seen:
        left = deadline - time.monotonic()
        if left <= 0:
            pytest.fail(f"timed out waiting for {until!r}:\n{seen.decode('utf-8', 'replace')}")
        if not select.select([fd], [], [], left)[0]:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:  # EIO: the child's end of the pty is gone
            chunk = b""
        if not chunk:
            if until is None:
                return seen
            pytest.fail(f"terminal closed before {until!r}:\n{seen.decode('utf-8', 'replace')}")
        seen += chunk
    return seen


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX terminal")
def test_a_dialog_reads_dev_tty_where_kqueue_refuses_it(tmp_path) -> None:
    import fcntl
    import struct
    import termios

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    answer = tmp_path / "answer"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(answer)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=REPO_ROOT,
        env={**os.environ, "TERM": "xterm"},
        start_new_session=True,
    )
    os.close(slave)
    screen = b""
    try:
        # The switch to the alternate screen is the first draw: raw mode is
        # on and the terminal attached to the loop by then. (Not the label
        # text: a crash's traceback quotes the child's source, label and all.)
        screen = _read_terminal(master, b"\x1b[?1049h")
        os.write(master, b"\r")  # Enter picks the highlighted row
        screen += _read_terminal(master, None)
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    assert rc == 0, screen.decode("utf-8", "replace")
    assert answer.read_text(encoding="utf-8") == "('b', 'advance')"
