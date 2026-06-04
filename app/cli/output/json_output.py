"""JSON output helpers."""

from __future__ import annotations

import json
import sys
from typing import Any


def print_json(data: Any) -> None:
    """Pretty-print `data` as JSON to stdout."""
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def print_jsonl(data: Any) -> None:
    """Print a single compact JSON line — for streamed events."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def print_raw_bytes(b: bytes) -> None:
    """Write raw bytes (e.g. a verbatim JSON payload from the server)."""
    if not b:
        sys.stdout.write("\n")
        return
    sys.stdout.buffer.write(b)
    if not b.endswith(b"\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
