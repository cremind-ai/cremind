"""Parse Google Calendar event resources; render list rows / event markdown;
build event bodies for create/update."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


def _yaml_quote(value: Any) -> str:
    s = "" if value is None else str(value)
    if s == "":
        return '""'
    needs = any(c in s for c in ":#&*!|>'\"%@`\\") or s.startswith(("-", "?", "!"))
    needs = needs or "\n" in s or s.strip() != s
    if not needs:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _yaml_list(values) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_quote(v) for v in values) + "]"


def received_at_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _when(point: dict[str, Any]) -> str:
    return point.get("dateTime") or point.get("date") or ""


def parse_event(ev: dict[str, Any], *, calendar: str = "") -> dict[str, Any]:
    start = ev.get("start", {}) or {}
    end = ev.get("end", {}) or {}
    return {
        "id": ev.get("id", ""),
        "calendar": calendar,
        "status": ev.get("status", ""),
        "summary": ev.get("summary", ""),
        "description": ev.get("description", ""),
        "location": ev.get("location", ""),
        "start": _when(start),
        "end": _when(end),
        "all_day": "date" in start,
        "organizer": (ev.get("organizer", {}) or {}).get("email", ""),
        "attendees": [a.get("email", "") for a in (ev.get("attendees", []) or [])],
        "recurrence": ", ".join(ev.get("recurrence", []) or []),
        "html_link": ev.get("htmlLink", ""),
        "updated": ev.get("updated", ""),
    }


def format_event_markdown(ev: dict[str, Any], *, event_type: str = "event_changed", calendar: str = "") -> str:
    m = parse_event(ev, calendar=calendar) if "start" in ev and isinstance(ev.get("start"), dict) else ev
    description = str(m.get("description") or "")
    lines = [
        "---",
        f"id: {_yaml_quote(m.get('id', ''))}",
        f"calendar: {_yaml_quote(m.get('calendar', ''))}",
        f"status: {_yaml_quote(m.get('status', ''))}",
        f"summary: {_yaml_quote(m.get('summary', ''))}",
        f"start: {_yaml_quote(m.get('start', ''))}",
        f"end: {_yaml_quote(m.get('end', ''))}",
        f"all_day: {'true' if m.get('all_day') else 'false'}",
        f"location: {_yaml_quote(m.get('location', ''))}",
        f"organizer: {_yaml_quote(m.get('organizer', ''))}",
        f"attendees: {_yaml_list(m.get('attendees') or [])}",
        f"recurrence: {_yaml_quote(m.get('recurrence', ''))}",
        f"html_link: {_yaml_quote(m.get('html_link', ''))}",
        f"updated: {_yaml_quote(m.get('updated', ''))}",
        f"event_type: {_yaml_quote(event_type)}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        "---",
        "",
        description.rstrip(),
        "",
    ]
    return "\n".join(lines)


def format_events(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no events)"
    parts = []
    for ev in rows:
        r = parse_event(ev) if isinstance(ev.get("start"), dict) else ev
        block = [f"- {r.get('summary') or '(no summary)'}"]
        if r.get("start"):
            block.append(f"  When: {r['start']}" + (f" -> {r['end']}" if r.get("end") else ""))
        if r.get("location"):
            block.append(f"  Where: {r['location']}")
        block.append(f"  Id: {r.get('id', '')}")
        parts.append("\n".join(block))
    return "\n".join(parts)


def format_calendars(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no calendars)"
    parts = []
    for c in rows:
        marker = " (primary)" if c.get("primary") else ""
        parts.append(f"- {c.get('summary', '?')}{marker}\n  id: {c.get('id', '')}")
    return "\n".join(parts)


def _is_date_only(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-"


def build_event_body(
    *,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
    attendees: list[str] | None = None,
    all_day: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if location is not None:
        body["location"] = location
    if description is not None:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    if start is not None:
        if all_day or _is_date_only(start):
            body["start"] = {"date": start}
        else:
            body["start"] = {"dateTime": start}
    if end is not None:
        if all_day or _is_date_only(end):
            # RFC 5545 all-day end is exclusive; accept an inclusive end and +1 day.
            try:
                d = date.fromisoformat(end) + timedelta(days=1)
                body["end"] = {"date": d.isoformat()}
            except ValueError:
                body["end"] = {"date": end}
        else:
            body["end"] = {"dateTime": end}
    return body
