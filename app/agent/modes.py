"""Agent turn modes: plan / reasoning / instant.

The chat UI/CLI send a per-request ``mode`` selecting how the agent behaves for
the turn:

* ``reasoning`` — the default; today's behavior (extended thinking + the hidden
  ``reasoning`` think-tool for models that lack native reasoning).
* ``instant`` — fastest: extended thinking is suppressed for the turn and the
  think-tool + its guidance are dropped.
* ``plan`` — Claude-Code-style plan mode: research read-only, ask clarifying
  questions, write a plan file for approval, then execute with a live todo list.

Back-compat: older clients send only the ``reasoning`` boolean. When ``mode`` is
absent — or an unknown value from an old/foreign client — it is derived from that
boolean (``reasoning=False`` → ``instant``, else ``reasoning``) rather than
raising, so a bad ``mode`` never 400s a request. ``plan`` is never derivable from
the boolean; it must be sent explicitly.
"""

from __future__ import annotations

from typing import Optional

AGENT_MODES = ("plan", "reasoning", "instant")

DEFAULT_MODE = "reasoning"


def normalize_mode(mode: Optional[str], *, reasoning: bool = True) -> str:
    """Return a valid mode string, deriving from the legacy ``reasoning`` bool."""
    if mode in AGENT_MODES:
        return mode  # type: ignore[return-value]
    return "reasoning" if reasoning else "instant"
