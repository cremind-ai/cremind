"""Cross-platform raw-mode terminal context manager.

Mirrors what `golang.org/x/term.MakeRaw` does in the Go CLI's `proc attach`.
Stdlib only — no `pywinpty` on the client side.

Usage:

    if is_stdin_tty():
        with raw_terminal():
            # stdin yields bytes verbatim (no line-buffering, no echo)
            chunk = os.read(0, 4096)
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator


def is_stdin_tty() -> bool:
    """True when stdin is attached to a terminal."""
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError):
        return False


def is_stdout_tty() -> bool:
    """True when stdout is attached to a terminal."""
    try:
        return os.isatty(sys.stdout.fileno())
    except (OSError, ValueError):
        return False


def get_terminal_size() -> tuple[int, int]:
    """Return `(cols, rows)`. Falls back to `(80, 24)` outside a terminal."""
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
        return sz.columns, sz.lines
    except (OSError, ValueError):
        try:
            sz = os.get_terminal_size()
            return sz.columns, sz.lines
        except (OSError, ValueError):
            return 80, 24


if sys.platform == "win32":
    @contextmanager
    def raw_terminal() -> Iterator[None]:
        """Put the local console into raw + VT-input mode and restore on exit.

        Uses `kernel32.SetConsoleMode` directly (no third-party deps). Modern
        Windows Terminal and ConHost both honor `ENABLE_VIRTUAL_TERMINAL_INPUT`,
        which makes arrow keys arrive as `ESC [ A` etc. — exactly the bytes
        the remote PTY expects.
        """
        import ctypes
        from ctypes import wintypes

        STD_INPUT_HANDLE = -10
        STD_OUTPUT_HANDLE = -11

        ENABLE_PROCESSED_INPUT = 0x0001
        ENABLE_LINE_INPUT = 0x0002
        ENABLE_ECHO_INPUT = 0x0004
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        DISABLE_NEWLINE_AUTO_RETURN = 0x0008

        kernel32 = ctypes.windll.kernel32

        hin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        hout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

        old_in = wintypes.DWORD()
        old_out = wintypes.DWORD()
        if not kernel32.GetConsoleMode(hin, ctypes.byref(old_in)):
            # Stdin isn't a real console; nothing to do.
            yield
            return
        kernel32.GetConsoleMode(hout, ctypes.byref(old_out))

        new_in = (
            old_in.value
            & ~(ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
        ) | ENABLE_VIRTUAL_TERMINAL_INPUT
        new_out = (
            old_out.value
            | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            | DISABLE_NEWLINE_AUTO_RETURN
        )

        kernel32.SetConsoleMode(hin, new_in)
        kernel32.SetConsoleMode(hout, new_out)
        try:
            yield
        finally:
            kernel32.SetConsoleMode(hin, old_in)
            kernel32.SetConsoleMode(hout, old_out)
else:
    @contextmanager
    def raw_terminal() -> Iterator[None]:
        """Put stdin into raw mode via termios; restore on exit."""
        import termios
        import tty

        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            # Stdin isn't a TTY; nothing to do.
            yield
            return
        try:
            tty.setraw(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
