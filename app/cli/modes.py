"""Agent turn mode enum for the CLI.

Defined here (stdlib-only) so it is importable at command-module import time —
Typer evaluates option signatures when ``app/cli/main.py`` imports the command
modules, and the CLI must not import from ``app.agent`` / ``app.constants`` /
etc. (see the import-discipline docstring in ``app/cli/main.py``). The values
mirror ``app.agent.modes.AGENT_MODES``; keep them in sync.
"""

from __future__ import annotations

from enum import Enum


class ChatMode(str, Enum):
    plan = "plan"
    reasoning = "reasoning"
    instant = "instant"
