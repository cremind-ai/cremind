"""Console + output mode helpers."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from rich.console import Console

from app.cli.config import Config


@dataclass(frozen=True)
class OutputMode:
    """Resolved output mode (whether to emit JSON, whether to suppress color).

    Mirrors the Go `output.Mode` struct produced by `output.FromConfig`.
    """

    json: bool
    no_color: bool

    @classmethod
    def from_config(cls, cfg: Config) -> "OutputMode":
        return cls(json=cfg.output == "json", no_color=cfg.no_color)


def get_console(mode: OutputMode) -> Console:
    """Return a rich Console honoring the user's color preference."""
    return Console(
        no_color=mode.no_color,
        soft_wrap=False,
        highlight=False,
        force_terminal=None,
    )


def is_tty() -> bool:
    """True when stdout is attached to a terminal."""
    try:
        return sys.stdout.isatty()
    except Exception:
        return False
