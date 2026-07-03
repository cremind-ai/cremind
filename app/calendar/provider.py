"""Calendar provider seam.

A thin abstraction over "where calendar events live + who fires their triggers".
Both the agent's ``scheduler`` action subtools and the Calendar & Schedule UI
go through a provider, so the storage/manager wiring lives in exactly one place.

- :class:`InternalCalendarProvider` (the default) is backed by
  ``schedule_event_subscriptions`` and the :class:`ScheduleManager`. It is fully
  functional on its own — create/list/update/delete events, one-shot or
  recurring, reminders or agent actions.
- A ``GoogleCalendarProvider`` is planned for a later phase. Even then, the
  *trigger* engine stays internal (Google can store events and remind, but it
  cannot run a Cremind agent action), so a local schedule-event mirror always
  owns firing.

:func:`get_calendar_provider` returns the active provider for a profile. Today
that is always the internal one.
"""

from __future__ import annotations

import abc
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from app.calendar import recurrence as R
from app.storage import get_schedule_event_storage
from app.utils.logger import logger

DEFAULT_DURATION_MINUTES = 30
# How far before the visible window to expand, so multi-day events that started
# earlier but still overlap the window are rendered.
_MULTIDAY_LOOKBACK_DAYS = 30


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ── Mapping the `scheduler` parser result -> row insert specs ───────────────

def schedule_specs_from_parser_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Translate a ``scheduler`` (parser) ``compute_schedule`` result into one or
    more row specs (dtstart / duration_minutes / rrule / recurrence_end_*).

    A recurrence or single point is one spec; an explicit set fans out to one
    one-shot spec per listed occurrence. Query windows / pure constraints are
    not bookable and yield no specs.
    """
    kind = result.get("schedule_kind")
    specs: List[Dict[str, Any]] = []

    if kind == "instant":
        inst = result.get("instant") or {}
        dt = inst.get("datetime")
        if dt:
            specs.append({
                "schedule_kind": "instant",
                "dtstart": dt,
                "duration_minutes": int(result.get("default_duration_minutes", DEFAULT_DURATION_MINUTES)),
                "rrule": None,
            })
    elif kind == "interval":
        iv = result.get("interval") or {}
        if iv.get("start"):
            duration = int(iv.get("duration_minutes", DEFAULT_DURATION_MINUTES))
            specs.append({
                "schedule_kind": "interval",
                "dtstart": iv["start"],
                "duration_minutes": duration,
                "rrule": None,
                # A midnight-anchored span of a whole day or more (e.g. "a trip
                # from today until 3 days later") is an all-day, multi-day event.
                "all_day": _looks_all_day(iv["start"], duration),
            })
    elif kind == "recurrence":
        rec = result.get("recurrence") or {}
        rrule = rec.get("rrule")
        dtstart = rec.get("dtstart")
        if not dtstart:
            # Open-ended rule with no explicit time-of-day anchor: start now.
            dtstart = R.format_local(_now())
        end = rec.get("recurrence_end") or {"type": "never"}
        end_type = end.get("type")
        end_value = end.get("value")
        specs.append({
            "schedule_kind": "recurrence",
            "dtstart": dtstart,
            "duration_minutes": int(rec.get("duration_minutes", DEFAULT_DURATION_MINUTES)),
            "rrule": rrule,
            "recurrence_end_type": end_type,
            "recurrence_end_value": str(end_value) if end_value is not None else None,
        })
    elif kind == "explicit_set":
        for occ in (result.get("explicit_set") or {}).get("occurrences", []):
            start = occ.get("instant") or occ.get("start")
            if not start:
                continue
            duration = DEFAULT_DURATION_MINUTES
            if occ.get("start") and occ.get("end"):
                duration = int(occ.get("duration_minutes") or _minutes_between(occ["start"], occ["end"]))
            specs.append({
                "schedule_kind": "instant",
                "dtstart": start,
                "duration_minutes": duration,
                "rrule": None,
            })
    # window / constraint: nothing to book.
    return specs


def _minutes_between(start_iso: str, end_iso: str) -> int:
    try:
        delta = R.parse_local(end_iso) - R.parse_local(start_iso)
        return max(1, int(delta.total_seconds() // 60))
    except Exception:  # noqa: BLE001
        return DEFAULT_DURATION_MINUTES


def _looks_all_day(dtstart_iso: str, duration_minutes: int) -> bool:
    """Heuristic: a span of ≥ 1 whole day (a multi-day event like a trip) reads
    as all-day for display/sync purposes. Intra-day spans stay timed."""
    return int(duration_minutes) >= 1440


# ── Provider interface ──────────────────────────────────────────────────────

class CalendarProvider(abc.ABC):
    """CRUD over calendar events for one profile, plus connection status."""

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """True for an external provider that has completed auth. The internal
        default is always 'connected' (it is the system calendar)."""

    @abc.abstractmethod
    def create_event(self, **kwargs: Any) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def update_event(self, event_id: str, **fields: Any) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def delete_event(self, event_id: str) -> bool: ...

    @abc.abstractmethod
    def set_status(self, event_id: str, status: str) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def list_subscriptions(self, profile: str) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def list_occurrences(
        self, profile: str, range_start: str, range_end: str,
    ) -> List[Dict[str, Any]]: ...


class InternalCalendarProvider(CalendarProvider):
    """Default provider — system calendar backed by schedule_event_subscriptions."""

    name = "internal"

    def __init__(self) -> None:
        self._store = get_schedule_event_storage()

    def is_connected(self) -> bool:
        return True

    def _manager(self):
        from app.events import get_schedule_manager
        return get_schedule_manager()

    def _seed_next_fire_at(self, *, schedule_kind: str, dtstart: str, rrule: Optional[str],
                           recurrence_end_type: Optional[str], recurrence_end_value: Optional[str]) -> Optional[float]:
        """Compute the initial rolling-pointer epoch, or None if the event is
        wholly in the past (a one-shot whose time has passed)."""
        now = _now()
        until = recurrence_end_value if recurrence_end_type == "until" else None
        if rrule:
            occ = R.first_occurrence_on_or_after(rrule=rrule, dtstart=dtstart, moment=now, until=until)
            return R.to_epoch(occ) if occ else None
        start = R.parse_local(dtstart)
        return R.to_epoch(start) if start >= now else None

    def create_event(
        self,
        *,
        profile: str,
        conversation_id: str,
        title: str,
        action: str = "",
        source: str = "agent",
        schedule_kind: str = "instant",
        dtstart: str,
        duration_minutes: int = DEFAULT_DURATION_MINUTES,
        all_day: bool = False,
        rrule: Optional[str] = None,
        recurrence_end_type: Optional[str] = None,
        recurrence_end_value: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> Dict[str, Any]:
        next_fire_at = self._seed_next_fire_at(
            schedule_kind=schedule_kind, dtstart=dtstart, rrule=rrule,
            recurrence_end_type=recurrence_end_type, recurrence_end_value=recurrence_end_value,
        )
        # A past one-shot is stored as a completed calendar record (never fires).
        status = "active" if next_fire_at is not None else "completed"
        # Every schedule event runs an action; when none is given, the title is
        # the command (so a bare "tắt đèn hiên" still executes).
        action = (action or "").strip() or (title or "").strip()
        row = self._store.insert(
            conversation_id=conversation_id,
            profile=profile,
            title=title or "",
            action=action,
            all_day=bool(all_day),
            schedule_kind=schedule_kind,
            dtstart=dtstart,
            duration_minutes=int(duration_minutes),
            next_fire_at=next_fire_at,
            rrule=rrule,
            recurrence_end_type=recurrence_end_type,
            recurrence_end_value=recurrence_end_value,
            timezone=timezone,
            status=status,
            source=source,
        )
        if status == "active":
            self._manager().arm(row)
        logger.info(
            f"[calendar] created schedule event {row['id']} title={title!r} "
            f"kind={schedule_kind} rrule={rrule!r} next_fire_at={next_fire_at} status={status}"
        )
        return row

    def create_from_parser_result(
        self,
        *,
        profile: str,
        conversation_id: str,
        title: str,
        action: str = "",
        source: str = "agent",
        result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Create one or more events from a ``scheduler`` parser result."""
        specs = schedule_specs_from_parser_result(result)
        rows: List[Dict[str, Any]] = []
        for spec in specs:
            rows.append(self.create_event(
                profile=profile, conversation_id=conversation_id, title=title,
                action=action, source=source,
                schedule_kind=spec["schedule_kind"], dtstart=spec["dtstart"],
                duration_minutes=spec.get("duration_minutes", DEFAULT_DURATION_MINUTES),
                all_day=spec.get("all_day", False),
                rrule=spec.get("rrule"),
                recurrence_end_type=spec.get("recurrence_end_type"),
                recurrence_end_value=spec.get("recurrence_end_value"),
            ))
        return rows

    def update_event(self, event_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        existing = self._store.get(event_id)
        if existing is None:
            return None
        # If timing-relevant fields changed, recompute the rolling pointer.
        timing_keys = {"dtstart", "rrule", "recurrence_end_type", "recurrence_end_value"}
        if timing_keys & set(fields.keys()):
            merged = {**existing, **fields}
            new_next = self._seed_next_fire_at(
                schedule_kind=merged.get("schedule_kind", "instant"),
                dtstart=merged["dtstart"], rrule=merged.get("rrule"),
                recurrence_end_type=merged.get("recurrence_end_type"),
                recurrence_end_value=merged.get("recurrence_end_value"),
            )
            fields["next_fire_at"] = new_next
            if merged.get("status") != "cancelled":
                fields["status"] = "active" if new_next is not None else "completed"
        row = self._store.update_fields(event_id, **fields)
        if row is not None:
            self._manager().refresh(event_id)
        return row

    def delete_event(self, event_id: str) -> bool:
        ok = self._store.delete(event_id)
        if ok:
            self._manager().remove(event_id)
        return ok

    def set_status(self, event_id: str, status: str) -> Optional[Dict[str, Any]]:
        existing = self._store.get(event_id)
        if existing is None:
            return None
        next_fire_at = existing.get("next_fire_at")
        if status == "active":
            # Resume: re-seed the pointer from now so a long-paused rule lands on
            # its next future occurrence rather than a stale past one.
            next_fire_at = self._seed_next_fire_at(
                schedule_kind=existing.get("schedule_kind", "instant"),
                dtstart=existing["dtstart"], rrule=existing.get("rrule"),
                recurrence_end_type=existing.get("recurrence_end_type"),
                recurrence_end_value=existing.get("recurrence_end_value"),
            )
            if next_fire_at is None:
                status = "completed"
        else:
            next_fire_at = None
        self._store.set_status(event_id, status, next_fire_at=next_fire_at)
        self._manager().refresh(event_id)
        return self._store.get(event_id)

    def list_subscriptions(self, profile: str) -> List[Dict[str, Any]]:
        return self._store.list_by_profile(profile)

    def list_occurrences(
        self, profile: str, range_start: str, range_end: str,
    ) -> List[Dict[str, Any]]:
        """Expand every subscription into concrete occurrences within the window.

        Recurrences are expanded on demand for the visible range only; nothing is
        persisted. Cancelled rows are skipped; completed/active are included so
        past one-shots still show on the calendar.
        """
        return self._expand_subscriptions(
            self._store.list_by_profile(profile), range_start, range_end,
        )

    def _expand_subscriptions(
        self, subs: List[Dict[str, Any]], range_start: str, range_end: str,
    ) -> List[Dict[str, Any]]:
        """Expand the given subscription rows into occurrences within the window.

        Shared by :meth:`list_occurrences` and :class:`GoogleCalendarProvider`,
        which merges local-only rows alongside the events it fetches from Google.
        """
        rs = R.parse_local(range_start)
        re_ = R.parse_local(range_end)
        # Widen the expansion start so a multi-day event that began before the
        # visible window but still overlaps it is rendered. (One-shot multi-day
        # spans are expanded by their dtstart, which may sit just before rs.)
        rs_expand = rs - timedelta(days=_MULTIDAY_LOOKBACK_DAYS)
        out: List[Dict[str, Any]] = []
        for sub in subs:
            if sub.get("status") == "cancelled":
                continue
            until = sub.get("recurrence_end_value") if sub.get("recurrence_end_type") == "until" else None
            dur = timedelta(minutes=int(sub.get("duration_minutes", DEFAULT_DURATION_MINUTES)))
            occs = R.expand_occurrences(
                rrule=sub.get("rrule"), dtstart=sub["dtstart"],
                range_start=rs_expand, range_end=re_, until=until,
            )
            for occ in occs:
                end = occ + dur
                # Drop occurrences that ended before the visible window starts.
                if end < rs:
                    continue
                out.append({
                    "subscription_id": sub["id"],
                    "title": sub.get("title", ""),
                    "action": sub.get("action", ""),
                    "all_day": sub.get("all_day", False),
                    "schedule_kind": sub.get("schedule_kind"),
                    "is_recurring": bool(sub.get("rrule")),
                    "rrule": sub.get("rrule"),
                    "status": sub.get("status"),
                    "source": sub.get("source"),
                    "conversation_id": sub.get("conversation_id"),
                    "start": R.format_local(occ),
                    "end": R.format_local(end),
                })
        out.sort(key=lambda e: e["start"])
        return out


# ── Google Calendar provider (Phase 2) ──────────────────────────────────────

class GoogleApiError(RuntimeError):
    pass


# Google Calendar recurrence supports only DAILY/WEEKLY/MONTHLY/YEARLY; sub-daily
# frequencies (hourly/minutely/secondly) are rejected by the API with HTTP 400.
_GOOGLE_UNSUPPORTED_FREQS = ("SECONDLY", "MINUTELY", "HOURLY")


def google_supports_rrule(rrule: Optional[str]) -> bool:
    """Whether ``rrule`` can be mirrored to Google Calendar. A one-shot (no
    rrule) is fine; a sub-daily ``FREQ`` (hourly/minutely/secondly) is not."""
    if not rrule:
        return True
    up = rrule.upper()
    return not any(f"FREQ={f}" in up for f in _GOOGLE_UNSUPPORTED_FREQS)


def _to_rfc3339_local(naive_iso: str) -> str:
    """Naive local wall-clock ISO -> RFC3339 with the machine's UTC offset."""
    return R.parse_local(naive_iso).astimezone().isoformat()


def _google_dt_to_local_iso(g: Optional[Dict[str, Any]]) -> Optional[str]:
    """A Google event start/end ({dateTime|date}) -> naive local 'YYYY-MM-DDTHH:MM:SS'."""
    if not g:
        return None
    dt_str = g.get("dateTime")
    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return R.format_local(dt)
    if g.get("date"):
        return f"{g['date']}T00:00:00"
    return None


def _google_event_body(
    *, title: str, dtstart: str, duration_minutes: int, rrule: Optional[str],
    action: str, all_day: bool = False,
) -> Dict[str, Any]:
    """Build a Google Calendar event body for a Cremind schedule event mirror."""
    start_dt = R.parse_local(dtstart)
    end_dt = start_dt + timedelta(minutes=int(duration_minutes or DEFAULT_DURATION_MINUTES))
    if all_day:
        # Google all-day events use date-only with an EXCLUSIVE end date. A span
        # of N×1440 minutes covers N days: end date = start date + N (≥1).
        days = max(1, -(-int(duration_minutes or 1440) // 1440))  # ceil
        start_date = start_dt.date()
        end_date = start_date + timedelta(days=days)
        body: Dict[str, Any] = {
            "summary": title or "Cremind event",
            "start": {"date": start_date.isoformat()},
            "end": {"date": end_date.isoformat()},
        }
    else:
        body = {
            "summary": title or "Cremind event",
            "start": {"dateTime": start_dt.astimezone().isoformat()},
            "end": {"dateTime": end_dt.astimezone().isoformat()},
        }
    if rrule:
        body["recurrence"] = [f"RRULE:{rrule}"]
    note = f"Cremind action: {action}" if action else "Cremind event"
    body["description"] = f"{note}\n(managed by Cremind)"
    return body


def _google_event_to_occurrence(ev: Dict[str, Any], match_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map a (single-instance) Google event to the UI occurrence dict.

    When the event was mirrored from a Cremind schedule (``match_row`` found via
    ``external_event_id``), surface the Cremind fields so it stays editable;
    otherwise it's a pure Google event rendered read-only.
    """
    start = _google_dt_to_local_iso(ev.get("start"))
    end = _google_dt_to_local_iso(ev.get("end")) or start
    g_recurring = bool(ev.get("recurringEventId") or ev.get("recurrence"))
    # Google marks an all-day event with date-only start/end (no dateTime).
    g_all_day = bool((ev.get("start") or {}).get("date"))
    if match_row:
        return {
            "subscription_id": match_row["id"],
            "title": match_row.get("title") or ev.get("summary", ""),
            "action": match_row.get("action", ""),
            "all_day": match_row.get("all_day", g_all_day),
            "schedule_kind": match_row.get("schedule_kind"),
            "is_recurring": bool(match_row.get("rrule")) or g_recurring,
            "rrule": match_row.get("rrule"),
            "status": match_row.get("status", "active"),
            "source": match_row.get("source", "agent"),
            "conversation_id": match_row.get("conversation_id"),
            "start": start,
            "end": end,
            "external": "google",
        }
    return {
        "subscription_id": None,
        "title": ev.get("summary", "(busy)"),
        "action": "",
        "all_day": g_all_day,
        "schedule_kind": "instant",
        "is_recurring": g_recurring,
        "rrule": None,
        "status": "active",
        "source": "google",
        "conversation_id": None,
        "start": start,
        "end": end,
        "external": "google",
        "read_only": True,
    }


class GoogleCalendarProvider(CalendarProvider):
    """Google-backed provider. Wraps the internal provider so Cremind's trigger
    engine still owns firing: every create/update/delete mutates the internal
    schedule-event row (ScheduleManager fires it) AND mirrors to Google (so it
    shows on the user's calendar). Reads come from Google so the view reflects
    the whole calendar; mirrored events reconcile back via ``external_event_id``.
    """

    name = "google"
    BASE = "https://www.googleapis.com/calendar/v3"
    CALENDAR_ID = "primary"

    def __init__(self, profile: str) -> None:
        self._profile = profile
        self._internal = InternalCalendarProvider()
        self._store = self._internal._store

    def is_connected(self) -> bool:
        from app.calendar import google_auth
        return google_auth.status(self._profile).get("connected", False)

    # ── REST helper ────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Dict[str, Any]:
        from app.calendar import google_auth
        token = google_auth.get_access_token(self._profile)
        if not token:
            raise GoogleApiError("Google Calendar is not connected for this profile")
        with httpx.Client(timeout=20.0) as client:
            resp = client.request(
                method, f"{self.BASE}{path}", params=params, json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 204:
                return {}
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    def _mirror_body(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return _google_event_body(
            title=row.get("title", ""), dtstart=row["dtstart"],
            duration_minutes=row.get("duration_minutes", DEFAULT_DURATION_MINUTES),
            rrule=row.get("rrule"),
            action=row.get("action", ""), all_day=row.get("all_day", False),
        )

    # ── CRUD (internal row is the source of truth for triggering) ───────
    def create_event(self, **kwargs: Any) -> Dict[str, Any]:
        row = self._internal.create_event(**kwargs)
        if not google_supports_rrule(row.get("rrule")):
            # Sub-daily recurrence Google can't store: keep it local-only (it still
            # fires and shows via the list_occurrences merge). No mirror attempt.
            logger.info(
                f"[google] event {row['id']} rrule={row.get('rrule')!r} is sub-daily; "
                "kept local-only (Google Calendar can't store it)"
            )
            return row
        try:
            ev = self._request("POST", f"/calendars/{self.CALENDAR_ID}/events", json_body=self._mirror_body(row))
            gid = ev.get("id")
            if gid:
                updated = self._store.update_fields(row["id"], external_provider="google", external_event_id=gid)
                if updated:
                    row = updated
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[google] mirror create failed (event still fires locally): {exc}")
        return row

    def update_event(self, event_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        row = self._internal.update_event(event_id, **fields)
        if row and row.get("external_event_id") and google_supports_rrule(row.get("rrule")):
            try:
                self._request(
                    "PATCH", f"/calendars/{self.CALENDAR_ID}/events/{row['external_event_id']}",
                    json_body=self._mirror_body(row),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[google] mirror update failed: {exc}")
        return row

    def delete_event(self, event_id: str) -> bool:
        row = self._store.get(event_id)
        ok = self._internal.delete_event(event_id)
        if ok and row and row.get("external_event_id"):
            try:
                self._request("DELETE", f"/calendars/{self.CALENDAR_ID}/events/{row['external_event_id']}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[google] mirror delete failed: {exc}")
        return ok

    def set_status(self, event_id: str, status: str) -> Optional[Dict[str, Any]]:
        row = self._store.get(event_id)
        result = self._internal.set_status(event_id, status)
        if status == "cancelled" and row and row.get("external_event_id"):
            try:
                self._request("DELETE", f"/calendars/{self.CALENDAR_ID}/events/{row['external_event_id']}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[google] mirror cancel/delete failed: {exc}")
        return result

    def list_subscriptions(self, profile: str) -> List[Dict[str, Any]]:
        # The Events page shows Cremind-managed triggers (internal rows), not raw
        # Google events — unchanged whether or not Google is connected.
        return self._internal.list_subscriptions(profile)

    def list_occurrences(self, profile: str, range_start: str, range_end: str) -> List[Dict[str, Any]]:
        rows = self._store.list_by_profile(profile)
        by_gid = {r["external_event_id"]: r for r in rows if r.get("external_event_id")}
        try:
            data = self._request(
                "GET", f"/calendars/{self.CALENDAR_ID}/events",
                params={
                    "timeMin": _to_rfc3339_local(range_start),
                    "timeMax": _to_rfc3339_local(range_end),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "250",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[google] events.list failed; showing internal occurrences: {exc}")
            return self._internal.list_occurrences(profile, range_start, range_end)
        out: List[Dict[str, Any]] = []
        for ev in data.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            match = by_gid.get(ev.get("recurringEventId") or "") or by_gid.get(ev.get("id") or "")
            occ = _google_event_to_occurrence(ev, match)
            if occ.get("start"):
                out.append(occ)
        # Merge local-only Cremind events (never mirrored to Google) so they still
        # show while connected — e.g. sub-daily reminders Google can't store, or
        # events created before connecting / whose mirror POST failed. Mirrored
        # rows stay represented by their Google copy above, so no double-counting.
        local_only = [r for r in rows if not r.get("external_event_id")]
        out.extend(self._internal._expand_subscriptions(local_only, range_start, range_end))
        out.sort(key=lambda e: e["start"])
        return out


_internal: Optional[InternalCalendarProvider] = None


def get_calendar_provider(profile: str) -> CalendarProvider:
    """Return the active calendar provider for ``profile``: Google when the
    profile has connected Google Calendar, else the internal/system calendar.
    The trigger engine is always internal regardless."""
    try:
        from app.calendar import google_auth
        if profile and google_auth.status(profile).get("connected"):
            return GoogleCalendarProvider(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[calendar] google status check failed, using internal: {exc}")
    global _internal
    if _internal is None:
        _internal = InternalCalendarProvider()
    return _internal
