"""Alerts as Cremind skill events.

Cremind watches ``events/<event_type>/`` and hands each new markdown file to every
subscription for that event type (an agent run that reports into the conversation
which subscribed). A file nobody is subscribed to is consumed and dropped by design.
"""

from __future__ import annotations

import errno
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import EVENTS_DIR

# The monitor's alert kinds and the event type each one raises.
ALERT_EVENT_TYPES = {
    "warn": "limit_warning",  # heads-up threshold crossed
    "crit": "switch_now",  # switch-now threshold crossed
    "eta": "limit_soon",  # a limit is minutes away at the current pace
    "limit": "limit_reached",
    "back": "account_available",  # a used-up account is available again
}
# Automatic switching's own events (monitor.py, planner.py).
AUTO_EVENT_TYPES = (
    "auto_switched",  # it switched your Claude Code to another account
    "no_account_available",  # the account in use reached a threshold and no account can take over
    "auto_switch_failed",  # it tried three times and could not switch
)
EVENT_TYPES = (*ALERT_EVENT_TYPES.values(), *AUTO_EVENT_TYPES)

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _sanitize(label: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")[:100].rstrip()
    if not cleaned:
        cleaned = "alert"
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def write_event(event_type: str, label: str, frontmatter: dict[str, Any], body: str, events_dir: Path = EVENTS_DIR) -> Path:
    """Write one event file into ``events/<event_type>/``.

    Cremind reads a file the moment it is created and ignores renames inside the skills
    folder, so the file is created exclusively and filled with a single write. The bytes
    are always valid UTF-8: Cremind's watcher stops on anything else.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"undeclared event type: {event_type}")
    folder = events_dir / event_type
    folder.mkdir(parents=True, exist_ok=True)
    fm = {k: v for k, v in frontmatter.items() if v is not None}
    fm["event_type"] = event_type
    fm.setdefault("received_at", datetime.now().astimezone().isoformat(timespec="seconds"))
    lines = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items())
    data = f"---\n{lines}\n---\n\n{body.strip()}\n".encode("utf-8", errors="replace")

    base = f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')} {_sanitize(label)}"
    attempt = 0
    while True:
        path = folder / (f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md")
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as e:
            if e.errno == errno.EEXIST and attempt < 1000:
                attempt += 1
                continue
            raise
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
        return path
