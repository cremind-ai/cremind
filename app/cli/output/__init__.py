"""Output helpers for the CLI.

Mirrors `cli/internal/output/output.go`. Keeps the surface small: tables that
auto-degrade to TSV when piped, aligned key/value blocks, and JSON helpers
for `--json` mode.
"""

from app.cli.output.console import OutputMode, get_console
from app.cli.output.json_output import print_json, print_jsonl, print_raw_bytes
from app.cli.output.tables import Table, print_kv, print_map

__all__ = [
    "OutputMode",
    "Table",
    "get_console",
    "print_json",
    "print_jsonl",
    "print_kv",
    "print_map",
    "print_raw_bytes",
]
