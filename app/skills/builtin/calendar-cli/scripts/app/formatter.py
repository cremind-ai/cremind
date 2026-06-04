"""Output formatting: YAML frontmatter, event markdown, plain-text tables."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _yaml_quote(value) -> str:
    if value is None:
        return '""'
    s = "" if value is None else str(value)
    if s == "":
        return '""'
    needs_quote = any(c in s for c in ":#&*!|>'\"%@`\\") or s.startswith(("-", "?", "!"))
    needs_quote = needs_quote or "\n" in s or s.strip() != s
    if not needs_quote:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_list(values) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_quote(v) for v in values) + "]"


def received_at_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def format_event_markdown(event: dict[str, Any], *, event_type: str) -> str:
    description = str(event.get("description") or "")
    lines = [
        "---",
        f"uid: {_yaml_quote(event.get('uid', ''))}",
        f"href: {_yaml_quote(event.get('href', ''))}",
        f"etag: {_yaml_quote(event.get('etag', ''))}",
        f"calendar: {_yaml_quote(event.get('calendar', ''))}",
        f"summary: {_yaml_quote(event.get('summary', ''))}",
        f"start: {_yaml_quote(event.get('start', ''))}",
        f"end: {_yaml_quote(event.get('end', ''))}",
        f"all_day: {'true' if event.get('all_day') else 'false'}",
        f"location: {_yaml_quote(event.get('location', ''))}",
        f"organizer: {_yaml_quote(event.get('organizer', ''))}",
        f"attendees: {_yaml_list(event.get('attendees') or [])}",
        f"status: {_yaml_quote(event.get('status', ''))}",
        f"recurrence: {_yaml_quote(event.get('recurrence', ''))}",
        f"event_type: {_yaml_quote(event_type)}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        "---",
        "",
        description.rstrip(),
        "",
    ]
    return "\n".join(lines)


def format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no events)"
    parts = []
    for r in rows:
        block = [
            f"- {r.get('summary') or '(no summary)'}",
        ]
        if r.get("start"):
            block.append(f"  When: {r['start']}" + (f" -> {r['end']}" if r.get('end') else ""))
        if r.get("location"):
            block.append(f"  Where: {r['location']}")
        block.append(f"  UID: {r.get('uid','')}")
        parts.append("\n".join(block))
    return "\n".join(parts)


def format_calendars_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no calendars)"
    parts = []
    for r in rows:
        marker = " (default)" if r.get("default") else ""
        parts.append(f"- {r.get('name', '?')}{marker}\n  {r.get('url','')}")
    return "\n".join(parts)
