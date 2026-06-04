"""Table rendering — TTY-aware, with TSV fallback when piped."""

from __future__ import annotations

import sys
from typing import Any, Iterable

from rich import box
from rich.console import Console
from rich.table import Table as RichTable

from app.cli.output.console import OutputMode, is_tty


class Table:
    """A small, TTY-aware table renderer.

    On a terminal with colors enabled, renders as an ASCII table via rich.
    When stdout is piped or `OPA_NO_COLOR` is set, renders as tab-separated
    values with no header underline, so output is grep/awk-friendly.
    """

    def __init__(self, mode: OutputMode, *headers: str) -> None:
        self.mode = mode
        self.headers: tuple[str, ...] = headers
        self.rows: list[list[str]] = []

    def add_row(self, *cells: Any) -> None:
        self.rows.append([_to_cell(c) for c in cells])

    def render(self) -> None:
        if is_tty() and not self.mode.no_color:
            console = Console(no_color=False, highlight=False, soft_wrap=False)
            tbl = RichTable(box=box.SQUARE, show_header=True, header_style="bold")
            for h in self.headers:
                tbl.add_column(h, overflow="fold")
            for row in self.rows:
                tbl.add_row(*row)
            console.print(tbl)
            return
        # Pipe-friendly TSV (no borders, no underlines).
        sys.stdout.write("\t".join(self.headers) + "\n")
        for row in self.rows:
            sys.stdout.write("\t".join(row) + "\n")


def print_kv(items: Iterable[tuple[str, str]]) -> None:
    """Print aligned `key: value` pairs, preserving insertion order."""
    items_list = list(items)
    width = max((len(k) for k, _ in items_list), default=0)
    for key, value in items_list:
        suffix = f"{key}:"
        sys.stdout.write(f"{suffix:<{width + 1}}  {value}\n")


def print_map(d: dict[str, Any]) -> None:
    """Print a dict as sorted `key: value` pairs."""
    print_kv((k, _to_cell(d[k])) for k in sorted(d))


def _to_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
