"""Behavior-based command classification for the Exec Shell tool.

Instead of classifying commands by name before execution, this module
provides tools for **runtime behaviour analysis**:

- TUI escape sequence detection in process output.
- Category enum for the three supported command types.

Classification happens by observing what the process *does*:

- Process exits on its own       → fire-and-forget
- TUI escape sequences in output → full-screen app (unsupported)
- Everything else                → long-running (supported)
"""

from __future__ import annotations

import re
from enum import Enum


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

class CommandCategory(str, Enum):
    """Process behaviour category for a shell command."""

    FIRE_AND_FORGET = "fire_and_forget"
    LONG_RUNNING = "long_running"
    TUI_FULLSCREEN = "tui_fullscreen"


UNSUPPORTED_CATEGORIES: frozenset[CommandCategory] = frozenset({
    CommandCategory.TUI_FULLSCREEN,
})


# ---------------------------------------------------------------------------
# TUI escape-sequence detection
# ---------------------------------------------------------------------------

# Strong TUI indicators — sequences that only full-screen apps emit.
_TUI_LITERAL_SEQUENCES: list[str] = [
    "\x1b[?1049h",   # Enable alternate screen buffer (xterm)
    "\x1b[?1049l",   # Disable alternate screen buffer
    "\x1b[?47h",     # Enable alternate screen buffer (older)
    "\x1b[?47l",     # Disable alternate screen buffer
    "\x1b[?1047h",   # Enable alternate screen buffer (variant)
    "\x1b[?1047l",   # Disable alternate screen buffer (variant)
    "\x1b[?25l",     # Hide cursor
    "\x1b[?1h",      # Application cursor-key mode
]

# Clear-screen immediately followed by cursor-home → strong TUI indicator.
_TUI_CLEAR_AND_HOME_RE = re.compile(r"\x1b\[2J\x1b\[(?:1;1)?H")

# Dense cursor positioning (e.g. ``\x1b[5;12H``) — TUI apps emit many of
# these in a single output chunk to redraw the screen.
_CURSOR_POSITION_RE = re.compile(r"\x1b\[\d+;\d+H")

# Minimum number of cursor-position sequences in a single chunk to flag
# the output as TUI.  Normal CLI tools rarely emit more than one or two.
_CURSOR_POS_THRESHOLD = 5


def detect_tui_sequences(output: str) -> bool:
    """Return ``True`` if *output* contains escape sequences typical of TUI apps.

    Checks for:
    - Alternate screen buffer activation / deactivation.
    - Cursor hiding.
    - Full-screen clear + cursor-home combination.
    - Dense cursor-positioning (≥ 5 ``\\e[row;colH`` in one chunk).

    Normal colour / style codes (``\\e[31m``, ``\\e[1m``, …) are **not**
    flagged — many CLI tools use those for coloured output.
    """
    for seq in _TUI_LITERAL_SEQUENCES:
        if seq in output:
            return True
    if _TUI_CLEAR_AND_HOME_RE.search(output):
        return True
    if len(_CURSOR_POSITION_RE.findall(output)) >= _CURSOR_POS_THRESHOLD:
        return True
    return False
