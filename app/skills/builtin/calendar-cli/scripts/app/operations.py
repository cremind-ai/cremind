"""High-level CRUD verbs. Each returns a JSON-serializable dict or list."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from . import ical
from .caldav_client import CalDAVClient, CalDAVError, event_not_found


def list_calendars() -> list[dict[str, Any]]:
    with CalDAVClient() as client:
        return client.calendar_info()


def list_events(
    *,
    calendar: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 50,
    detail: str = "summary",
) -> list[dict[str, Any]]:
    with CalDAVClient() as client:
        cal = client.find_calendar(calendar)
        cal_name = _name(cal)
        start_dt, end_dt = _resolve_window(since, before)

        results: list[dict[str, Any]] = []
        try:
            events = cal.search(
                start=start_dt,
                end=end_dt,
                event=True,
                expand=True,
            )
        except TypeError:
            # Older caldav library without the modern search signature.
            events = cal.date_search(start=start_dt, end=end_dt, expand=True)

        q = (query or "").strip().lower()
        for event in events:
            comp = _event_component(event)
            if comp is None:
                continue
            row = ical.parse_vevent_to_dict(comp, calendar_name=cal_name, detail=detail)
            if q:
                hay = " ".join(
                    str(row.get(k, "") or "") for k in ("summary", "location", "description")
                ).lower()
                if q not in hay:
                    continue
            results.append(row)
            if len(results) >= max_results:
                break
        return results


def get_event(*, uid: str, calendar: Optional[str] = None) -> dict[str, Any]:
    with CalDAVClient() as client:
        cal = client.find_calendar(calendar)
        cal_name = _name(cal)
        event = _find_by_uid(cal, uid)
        if event is None:
            raise event_not_found(uid)

        ical_bytes = _event_bytes(event)
        components = ical.find_all_vevents(ical_bytes)
        if not components:
            raise event_not_found(uid)

        master = next(
            (c for c in components if "RECURRENCE-ID" not in c),
            components[0],
        )
        result = ical.parse_vevent_to_dict(master, calendar_name=cal_name, detail="full")
        result["href"] = str(event.url)
        result["etag"] = event.etag or ""

        overrides = [c for c in components if "RECURRENCE-ID" in c]
        if overrides:
            result["recurrences"] = [
                ical.parse_vevent_to_dict(c, calendar_name=cal_name, detail="full")
                for c in overrides
            ]
        return result


def create_event(
    *,
    summary: str,
    start: str,
    end: str,
    all_day: bool = False,
    location: Optional[str] = None,
    description: Optional[str] = None,
    attendees: Optional[Iterable[str]] = None,
    calendar: Optional[str] = None,
) -> dict[str, Any]:
    with CalDAVClient() as client:
        cal = client.find_calendar(calendar)
        start_dt, end_dt = _parse_user_window(start, end, all_day=all_day)

        ical_bytes, uid = ical.build_vcalendar(
            summary=summary,
            start=start_dt,
            end=end_dt,
            all_day=all_day,
            location=location,
            description=description,
            attendees=attendees,
        )
        try:
            event = cal.save_event(ical=ical_bytes)
        except Exception as e:
            raise CalDAVError(f"Failed to create event: {e}") from e
        return {
            "ok": True,
            "uid": uid,
            "calendar": _name(cal),
            "href": str(event.url),
            "etag": event.etag or "",
            "summary": summary,
        }


def update_event(
    *,
    uid: str,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    all_day: Optional[bool] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    attendees: Optional[Iterable[str]] = None,
    calendar: Optional[str] = None,
) -> dict[str, Any]:
    with CalDAVClient() as client:
        cal = client.find_calendar(calendar)
        event = _find_by_uid(cal, uid)
        if event is None:
            raise event_not_found(uid)

        ical_bytes = _event_bytes(event)
        existing = ical.find_vevent(ical_bytes)
        if existing is None:
            raise event_not_found(uid)

        current = ical.parse_vevent_to_dict(existing, calendar_name=_name(cal), detail="full")

        new_summary = summary if summary is not None else current.get("summary", "")
        new_location = location if location is not None else current.get("location", "")
        new_description = description if description is not None else current.get("description", "")
        new_all_day = all_day if all_day is not None else bool(current.get("all_day", False))

        if start is not None or end is not None:
            new_start_raw = start if start is not None else current.get("start", "")
            new_end_raw = end if end is not None else current.get("end", "")
            new_start_dt, new_end_dt = _parse_user_window(new_start_raw, new_end_raw, all_day=new_all_day)
        else:
            dtstart_prop = existing.get("dtstart")
            dtend_prop = existing.get("dtend")
            new_start_dt = dtstart_prop.dt if dtstart_prop is not None else None
            new_end_dt = dtend_prop.dt if dtend_prop is not None else None

        new_attendees: Optional[list[str]] = None
        if attendees is not None:
            new_attendees = [a for a in attendees if a]
        else:
            new_attendees = current.get("attendees") or None

        new_bytes, _uid = ical.build_vcalendar(
            uid=uid,
            summary=new_summary,
            start=new_start_dt,
            end=new_end_dt,
            all_day=new_all_day,
            location=new_location or None,
            description=new_description or None,
            attendees=new_attendees,
            organizer=current.get("organizer") or None,
            status=current.get("status") or None,
            existing_event=existing,
        )

        try:
            event.data = new_bytes
            event.save()
        except Exception as e:
            raise CalDAVError(f"Failed to update event: {e}") from e

        return {
            "ok": True,
            "uid": uid,
            "calendar": _name(cal),
            "href": str(event.url),
            "etag": event.etag or "",
            "summary": new_summary,
        }


def delete_event(*, uid: str, calendar: Optional[str] = None) -> dict[str, Any]:
    with CalDAVClient() as client:
        cal = client.find_calendar(calendar)
        event = _find_by_uid(cal, uid)
        if event is None:
            raise event_not_found(uid)
        try:
            event.delete()
        except Exception as e:
            raise CalDAVError(f"Failed to delete event: {e}") from e
        return {"ok": True, "uid": uid, "calendar": _name(cal)}


def _resolve_window(since: Optional[str], before: Optional[str]) -> tuple[datetime, datetime]:
    """Default to today → +30 days when neither bound is given."""
    now = datetime.now(timezone.utc)
    if since is None and before is None:
        return now, now + timedelta(days=30)
    if since:
        start_dt = _parse_date(since).replace(tzinfo=timezone.utc) if _is_date_only(since) else _parse_iso(since)
    else:
        start_dt = now - timedelta(days=365)
    if before:
        end_dt = _parse_date(before).replace(tzinfo=timezone.utc) if _is_date_only(before) else _parse_iso(before)
    else:
        end_dt = start_dt + timedelta(days=365)
    return start_dt, end_dt


def _parse_user_window(start: str, end: str, *, all_day: bool):
    start_dt = ical.parse_user_datetime(start, all_day=all_day)
    if all_day:
        # User-supplied end is INCLUSIVE; convert to RFC 5545 exclusive.
        end_dt = ical.inclusive_to_exclusive_end_date(end)
    else:
        end_dt = ical.parse_user_datetime(end, all_day=False)
    return start_dt, end_dt


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def _parse_iso(s: str) -> datetime:
    from dateutil import parser as date_parser
    dt = date_parser.isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_date_only(s: str) -> bool:
    return len(s) == 10 and s[4] == "-" and s[7] == "-"


def _find_by_uid(cal, uid: str):
    try:
        event = cal.event_by_uid(uid)
        if event is not None:
            return event
    except Exception:
        pass
    # Fallback: walk all events and match UID inline.
    try:
        for event in cal.events():
            comp = _event_component(event)
            if comp is not None and str(comp.get("uid", "")) == uid:
                return event
    except Exception:
        pass
    return None


def _event_component(event):
    """Return the master VEVENT component from a caldav Event."""
    try:
        instance = event.icalendar_instance
    except Exception:
        try:
            return ical.find_vevent(_event_bytes(event))
        except Exception:
            return None
    for comp in instance.walk("VEVENT"):
        if "RECURRENCE-ID" not in comp:
            return comp
    for comp in instance.walk("VEVENT"):
        return comp
    return None


def _event_bytes(event) -> bytes:
    raw = event.data
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return raw


def _name(cal) -> str:
    name = getattr(cal, "name", None) or ""
    if name:
        return str(name)
    try:
        return str(cal.get_display_name() or cal.url)
    except Exception:
        return str(cal.url)


__all__ = [
    "list_calendars",
    "list_events",
    "get_event",
    "create_event",
    "update_event",
    "delete_event",
]
