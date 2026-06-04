"""Build and parse iCalendar VEVENT components.

All datetimes are normalized to UTC on the wire (avoids constructing
VTIMEZONE blocks). User-facing output converts back to the local TZ.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from dateutil import parser as date_parser
from dateutil import tz
from icalendar import Calendar, Event, vCalAddress, vText


PRODID = "-//Cremind//calendar-cli//EN"

LOCAL_TZ = tz.tzlocal()


def parse_user_datetime(s: str, all_day: bool = False) -> date | datetime:
    """Parse a user-supplied date/time string.

    For all-day: must be YYYY-MM-DD; returns a `date`.
    For timed: ISO 8601 with or without offset; returns tz-aware `datetime`
    (naive input is interpreted as local time).
    """
    if not s:
        raise ValueError("empty datetime string")
    if all_day:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError as e:
            raise ValueError(
                f"--all-day requires YYYY-MM-DD format, got {s!r}"
            ) from e
    dt = date_parser.isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def build_vcalendar(
    *,
    uid: Optional[str] = None,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    all_day: bool = False,
    location: Optional[str] = None,
    description: Optional[str] = None,
    organizer: Optional[str] = None,
    attendees: Optional[Iterable[str]] = None,
    status: Optional[str] = None,
    existing_event: Optional[Event] = None,
) -> tuple[bytes, str]:
    """Build a VCALENDAR wrapping a single VEVENT. Returns (ical_bytes, uid).

    If `existing_event` is given, its UID is reused and unspecified fields
    are preserved (used by `update`).
    """
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")

    event = Event()
    if existing_event is not None:
        # Carry over properties from the prior version unless overridden below.
        for key, value in existing_event.items():
            if key.upper() in {
                "DTSTART", "DTEND", "SUMMARY", "LOCATION", "DESCRIPTION",
                "ORGANIZER", "ATTENDEE", "STATUS", "DTSTAMP", "LAST-MODIFIED",
                "UID",
            }:
                continue
            event.add(key, value)
        final_uid = str(existing_event.get("uid", uid or _new_uid()))
    else:
        final_uid = uid or _new_uid()

    event.add("uid", final_uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("last-modified", datetime.now(timezone.utc))
    event.add("summary", summary)

    if all_day:
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()
        event.add("dtstart", start)
        event.add("dtend", end)
    else:
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise ValueError("timed events require datetime start and end")
        event.add("dtstart", _to_utc(start))
        event.add("dtend", _to_utc(end))

    if location:
        event.add("location", vText(location))
    if description:
        event.add("description", vText(description))
    if organizer:
        addr = vCalAddress(f"mailto:{organizer}")
        addr.params["cn"] = organizer
        event.add("organizer", addr)
    if attendees:
        for a in attendees:
            if not a:
                continue
            addr = vCalAddress(f"mailto:{a}")
            addr.params["cn"] = a
            addr.params["RSVP"] = "TRUE"
            addr.params["PARTSTAT"] = "NEEDS-ACTION"
            event.add("attendee", addr, encode=0)
    if status:
        event.add("status", status.upper())

    cal.add_component(event)
    return cal.to_ical(), final_uid


def _new_uid() -> str:
    return f"{uuid.uuid4()}@cremind.calendar-cli"


def parse_vevent_to_dict(
    event: Event,
    *,
    calendar_name: str,
    detail: str = "summary",
) -> dict[str, Any]:
    """Convert an icalendar Event component to a JSON-serializable dict."""
    uid = str(event.get("uid", ""))
    summary = _text(event.get("summary"))
    location = _text(event.get("location"))
    description = _text(event.get("description"))
    status = _text(event.get("status"))

    dtstart_prop = event.get("dtstart")
    dtend_prop = event.get("dtend")
    dtstart_val = dtstart_prop.dt if dtstart_prop is not None else None
    dtend_val = dtend_prop.dt if dtend_prop is not None else None
    all_day = isinstance(dtstart_val, date) and not isinstance(dtstart_val, datetime)

    start_iso, end_iso = _iso_pair(dtstart_val, dtend_val, all_day=all_day)

    organizer = ""
    org_prop = event.get("organizer")
    if org_prop is not None:
        organizer = _strip_mailto(str(org_prop))

    attendees: list[str] = []
    att_props = event.get("attendee") or []
    if not isinstance(att_props, list):
        att_props = [att_props]
    for a in att_props:
        s = _strip_mailto(str(a))
        if s:
            attendees.append(s)

    rrule_prop = event.get("rrule")
    recurrence = ""
    if rrule_prop is not None:
        try:
            recurrence = rrule_prop.to_ical().decode("utf-8") if hasattr(rrule_prop, "to_ical") else str(rrule_prop)
        except Exception:
            recurrence = str(rrule_prop)

    base: dict[str, Any] = {
        "uid": uid,
        "calendar": calendar_name,
        "summary": summary,
        "start": start_iso,
        "end": end_iso,
        "all_day": all_day,
    }
    if detail == "summary":
        if location:
            base["location"] = location
        return base

    base["location"] = location
    base["description"] = description
    base["organizer"] = organizer
    base["attendees"] = attendees
    base["status"] = status
    base["recurrence"] = recurrence
    return base


def find_vevent(ical_bytes: bytes) -> Optional[Event]:
    """Return the first VEVENT (master) inside an ICS payload."""
    cal = Calendar.from_ical(ical_bytes)
    for comp in cal.walk("VEVENT"):
        if "RECURRENCE-ID" in comp:
            continue
        return comp
    # No master found; return first VEVENT (override-only file, rare).
    for comp in cal.walk("VEVENT"):
        return comp
    return None


def find_all_vevents(ical_bytes: bytes) -> list[Event]:
    """Return all VEVENT components (master + recurrence overrides)."""
    cal = Calendar.from_ical(ical_bytes)
    return list(cal.walk("VEVENT"))


def _text(value) -> str:
    if value is None:
        return ""
    return str(value)


def _strip_mailto(s: str) -> str:
    s = s.strip()
    if s.lower().startswith("mailto:"):
        return s[len("mailto:"):]
    return s


def _iso_pair(start, end, *, all_day: bool) -> tuple[str, str]:
    if start is None:
        return "", ""
    if all_day:
        # For listener output we present inclusive end (UI-friendly):
        # RFC 5545 stores DTEND as exclusive, so subtract one day.
        from datetime import timedelta
        start_s = start.isoformat()
        end_s = ""
        if end is not None:
            inclusive_end = end - timedelta(days=1)
            end_s = inclusive_end.isoformat()
        return start_s, end_s
    # datetime — render in local TZ for user-friendliness, preserving offset.
    start_local = start.astimezone(LOCAL_TZ) if start.tzinfo else start.replace(tzinfo=LOCAL_TZ)
    end_local = end.astimezone(LOCAL_TZ) if (end and end.tzinfo) else (end.replace(tzinfo=LOCAL_TZ) if end else None)
    return (
        start_local.isoformat(timespec="seconds"),
        end_local.isoformat(timespec="seconds") if end_local else "",
    )


def inclusive_to_exclusive_end_date(s: str) -> date:
    """Convert a user-supplied inclusive all-day end date to the
    RFC 5545 exclusive form (one day after the last event day)."""
    from datetime import timedelta
    d = datetime.strptime(s, "%Y-%m-%d").date()
    return d + timedelta(days=1)


__all__ = [
    "build_vcalendar",
    "parse_vevent_to_dict",
    "find_vevent",
    "find_all_vevents",
    "parse_user_datetime",
    "inclusive_to_exclusive_end_date",
    "LOCAL_TZ",
]
