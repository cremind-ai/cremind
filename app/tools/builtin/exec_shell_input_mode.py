"""Runtime input-mode detection for the Exec Shell tool.

Classifies whether a long-running process is currently waiting for:

- ``text``      — typed characters ending in Enter (e.g. "Project name: ").
- ``selection`` — arrow-key / Tab / Space navigation over a menu
                  (Inquirer-style prompts, questionary, @clack, gum, fzf, …).
- ``unknown``   — not enough signal to decide.

The detector is escape-sequence-driven: prompt libraries emit a recognisable
cocktail of DEC private-mode toggles (``\\e[?25l``, ``\\e[?1h``,
``\\e[?2004h``, ``\\e[?1000h``), cursor-up + clear-line redraw loops,
pointer glyphs, reverse-video segments, and hint text. Those signals are
framework-agnostic.

Two-tier state:

- **Persistent flags** (``TerminalState``) — DEC private-mode toggles stick
  until the app flips them back. Updated by ``update_terminal_state`` on
  every stdout chunk.
- **Tail signals** — recomputed at read time over the last few KB of stdout,
  so the final on-screen prompt wins over stale history.

Non-PTY processes can't actually receive arrow-key bytes through a plain
pipe; the detector still runs for them but caps confidence so callers can
surface a hint to rerun with ``pty: true``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


# ---------------------------------------------------------------------------
# Persistent terminal state
# ---------------------------------------------------------------------------

@dataclass
class TerminalState:
    """DEC private-mode flags + last-chunk transient signals.

    Persistent flags (``cursor_visible`` etc.) stay set across chunks until
    the app toggles them. ``last_chunk_*`` fields are rewritten on every
    update to describe only the most recent chunk.
    """
    cursor_visible: bool = True
    app_cursor_keys: bool = False
    bracketed_paste: bool = False
    mouse_tracking: bool = False
    alt_screen: bool = False
    last_chunk_redraw: bool = False
    last_chunk_pointer: bool = False
    last_chunk_hint: bool = False
    last_chunk_reverse_video: bool = False


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# DEC private-mode toggle: CSI ? Pn (h|l).  Multiple params separated by ;.
_PRIVATE_MODE_RE = re.compile(r"\x1b\[\?([\d;]+)([hl])")

# Cursor-up / previous-line / clear-line sequences that mark a redraw loop.
# Accepts both erase-in-line (K) and erase-in-display (J) — Clack/@clack
# repaints with `\x1b[nA\x1b[J` rather than per-line `\x1b[K`.
_CURSOR_UP_RE = re.compile(r"\x1b\[\d*[AF]")
_CLEAR_LINE_RE = re.compile(r"\x1b\[[012]?[JK]")

# Strip-all-ANSI (CSI + OSC). Conservative — keeps printable glyphs intact.
_ANSI_STRIP_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"   # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC terminated by BEL or ST
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS / PM / APC / SOS
    r"|\x1b[@-_]"                 # Single-char ESC commands (e.g. ESC =)
)

# Pointer glyph at (possibly indented) start of a line. Includes:
#   ❯ › ▸ ▶ ● ○ ◉ ◯ ⦿ → ◻ ◼   (unicode — filled/empty radio + multi-select)
#   >                          (ASCII fallback inquirer uses on dumb terminals)
#   [x] [X] [*] [ ]            (multi-select checkbox)
# An optional gutter (`│ ┃ ║ ◆ ◇`) may sit between the leading indent and the
# glyph — Clack/@clack/prompts and gum render selections as `│  ● item`.
_POINTER_LINE_RE = re.compile(
    r"(?m)^[ \t]{0,8}"
    r"(?:[│┃║◆◇][ \t]{0,8})?"
    r"(?:[❯›▸▶●○◉◯⦿→◻◼]|>|\[[ xX\*]\])\s+\S"
)

# Reverse-video segment with actual content inside: ESC[7m … ESC[(0|27)m.
_REVERSE_VIDEO_RE = re.compile(r"\x1b\[7m[^\x1b]{1,200}\x1b\[(?:0|27)m")

# Hint-text phrases common across prompt libraries.
_HINT_TEXT_RE = re.compile(
    r"(?:arrow keys|use\s+(?:space|tab|arrow)|press\s+(?:tab|enter|space)"
    r"|↑|↓|←|→|<space>|<enter>|\(space to select\)|\(toggle\)|\(y/n\))",
    re.IGNORECASE,
)

# Text-prompt tail: line ending in ": ", "? ", "> ", "$ ", "› ", "❯ " with
# no further printable content afterwards.  Tolerates a trailing cursor-show
# or colour reset.
_TEXT_PROMPT_TAIL_RE = re.compile(
    r"(?:[:?>$›❯])\s*\Z"
)

# Clack active-prompt header: a line starting with `◆` (active) or `◇`
# (complete) followed by the prompt question.  @clack/prompts and every
# CLI built on it (create-astro, sv, openclaw, shadcn-ui, …) emit this.
_CLACK_HEADER_RE = re.compile(r"(?m)^[ \t]{0,8}[◆◇][ \t]+\S")


# Thresholds — centralised so they're easy to tune after dogfooding.
_SELECTION_SCORE_THRESHOLD = 3
_TEXT_SCORE_THRESHOLD = -2
_TAIL_WINDOW = 4096
_NON_PTY_CONFIDENCE_CAP = 0.5


# ---------------------------------------------------------------------------
# State update
# ---------------------------------------------------------------------------

def update_terminal_state(state: TerminalState, chunk: str) -> None:
    """Apply DEC mode toggles and last-chunk signals from *chunk* to *state*.

    Mutates *state* in place. Call once per stdout chunk, before writing it
    to disk, so persistent flags see every toggle even across log rotations.
    """
    for match in _PRIVATE_MODE_RE.finditer(chunk):
        params = match.group(1)
        on = match.group(2) == "h"
        for p in params.split(";"):
            if not p:
                continue
            try:
                code = int(p)
            except ValueError:
                continue
            if code == 25:
                state.cursor_visible = on  # ?25h shows, ?25l hides
            elif code == 1:
                state.app_cursor_keys = on
            elif code == 2004:
                state.bracketed_paste = on
            elif code in (1000, 1001, 1002, 1003, 1005, 1006, 1015):
                state.mouse_tracking = on
            elif code in (1047, 1049, 47):
                state.alt_screen = on

    # Cursor-up + clear-line in the same chunk → list repaint in place.
    ups = len(_CURSOR_UP_RE.findall(chunk))
    clears = len(_CLEAR_LINE_RE.findall(chunk))
    state.last_chunk_redraw = ups >= 1 and clears >= 1

    stripped = _ANSI_STRIP_RE.sub("", chunk)
    state.last_chunk_pointer = bool(_POINTER_LINE_RE.search(stripped))
    state.last_chunk_hint = bool(_HINT_TEXT_RE.search(stripped))
    state.last_chunk_reverse_video = bool(_REVERSE_VIDEO_RE.search(chunk))


# ---------------------------------------------------------------------------
# Tail-biased mode detection
# ---------------------------------------------------------------------------

def detect_input_mode(
    state: TerminalState,
    tail: str,
    is_pty: bool,
) -> dict:
    """Decide the process's current input mode from persistent + tail signals.

    Parameters
    ----------
    state:
        The persistent ``TerminalState`` maintained by ``update_terminal_state``.
    tail:
        The last few KB of merged stdout. Used for "what's on screen right now"
        signals (pointer glyphs, hint text, trailing prompt colon).
    is_pty:
        Whether the process is running under a real PTY. Non-PTY processes can't
        receive raw arrow-key bytes correctly, so confidence is capped.

    Returns
    -------
    dict with keys:
        - ``input_mode``: ``"text" | "selection" | "unknown"``
        - ``confidence``: float in [0, 1]
        - ``score``: signed int — positive skews selection, negative skews text.
        - ``signals``: list of str tags that fired, for debugging / UI display.
    """
    if len(tail) > _TAIL_WINDOW:
        tail = tail[-_TAIL_WINDOW:]
    stripped_tail = _ANSI_STRIP_RE.sub("", tail)

    score = 0
    signals: List[str] = []

    # --- Selection signals -----------------------------------------------
    if not state.cursor_visible:
        score += 1
        signals.append("cursor_hidden")
    if state.app_cursor_keys:
        score += 3
        signals.append("app_cursor_keys")
    if state.mouse_tracking:
        score += 3
        signals.append("mouse_tracking")
    if state.last_chunk_redraw:
        score += 2
        signals.append("redraw_loop")
    if state.last_chunk_reverse_video or _REVERSE_VIDEO_RE.search(tail):
        score += 1
        signals.append("reverse_video")

    if state.last_chunk_pointer or _POINTER_LINE_RE.search(stripped_tail):
        score += 2
        signals.append("pointer_glyph")
    if state.last_chunk_hint or _HINT_TEXT_RE.search(stripped_tail):
        score += 3
        signals.append("key_hint_text")
    if _CLACK_HEADER_RE.search(stripped_tail):
        score += 2
        signals.append("clack_prompt_header")

    # --- Text signals ----------------------------------------------------
    if state.cursor_visible and _TEXT_PROMPT_TAIL_RE.search(stripped_tail.rstrip()):
        score -= 2
        signals.append("prompt_tail")
    if state.bracketed_paste and not state.app_cursor_keys:
        # Readline-style line editors (psql, python, node) flip bracketed-paste
        # but never app-cursor-keys.
        score -= 1
        signals.append("bracketed_paste")

    # --- Decision --------------------------------------------------------
    if score >= _SELECTION_SCORE_THRESHOLD:
        mode = "selection"
        confidence = min(1.0, 0.4 + 0.15 * (score - _SELECTION_SCORE_THRESHOLD + 1))
    elif score <= _TEXT_SCORE_THRESHOLD:
        mode = "text"
        confidence = min(1.0, 0.4 + 0.2 * (_TEXT_SCORE_THRESHOLD - score + 1))
    else:
        mode = "unknown"
        confidence = 0.2

    if not is_pty:
        # Without a TTY the app usually can't even read arrow keys; keep the
        # classification for telemetry but flag uncertainty so callers can
        # recommend pty=true.
        confidence = min(confidence, _NON_PTY_CONFIDENCE_CAP)
        signals.append("no_pty")

    return {
        "input_mode": mode,
        "confidence": round(confidence, 2),
        "score": score,
        "signals": signals,
    }
