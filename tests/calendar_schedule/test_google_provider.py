"""Tests for GoogleCalendarProvider mapping/mirror/reconcile + route presence.

The Calendar REST + token are mocked; the internal trigger store is faked, so
these are pure-logic tests (no DB, no network, no Google)."""

from __future__ import annotations

import app.calendar.provider as P


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_event_body_recurring():
    body = P._google_event_body(
        title="Standup", dtstart="2026-06-22T09:00:00", duration_minutes=30,
        rrule="FREQ=DAILY", action="check email",
    )
    assert body["summary"] == "Standup"
    assert body["recurrence"] == ["RRULE:FREQ=DAILY"]
    assert "dateTime" in body["start"] and "dateTime" in body["end"]
    assert "Cremind action: check email" in body["description"]


def test_event_body_no_action_no_recurrence():
    body = P._google_event_body(
        title="Dentist", dtstart="2026-06-22T15:00:00", duration_minutes=60,
        rrule=None, action="",
    )
    assert "recurrence" not in body
    assert "Cremind event" in body["description"]


def test_event_body_with_action_notes_command():
    body = P._google_event_body(
        title="Standup", dtstart="2026-06-22T09:00:00", duration_minutes=30,
        rrule=None, action="join the call",
    )
    assert "Cremind action: join the call" in body["description"]


def test_google_dt_to_local_iso_allday():
    assert P._google_dt_to_local_iso({"date": "2026-06-22"}) == "2026-06-22T00:00:00"


def test_event_body_all_day_uses_date_range():
    # 3-day trip = 3*1440 minutes, midnight start -> Google date-only, end EXCLUSIVE.
    body = P._google_event_body(
        title="Trip", dtstart="2026-06-22T00:00:00", duration_minutes=3 * 1440,
        rrule=None, action="", all_day=True,
    )
    assert body["start"] == {"date": "2026-06-22"}
    assert body["end"] == {"date": "2026-06-25"}  # start + 3 days, exclusive
    assert "dateTime" not in body["start"]


def test_specs_mark_all_day_for_midnight_multiday_interval():
    from app.utils.schedule import compute_schedule
    NOW = "2026-06-20T14:30:00"
    # "a trip from today until 3 days later" -> interval, midnight..+3d, no time.
    res = compute_schedule({
        "parsable": True, "schedule_kind": "interval",
        "time_elements": [
            {"mode": "relative", "time_range": "start", "offset_unit": "day", "offset_value": 0},
            {"mode": "relative", "time_range": "end", "offset_unit": "day", "offset_value": 3},
        ],
    }, NOW)
    specs = P.schedule_specs_from_parser_result(res)
    assert len(specs) == 1
    assert specs[0]["all_day"] is True
    assert specs[0]["duration_minutes"] >= 1440


def test_occurrence_match_surfaces_cremind_fields():
    ev = {"id": "gabc", "summary": "G",
          "start": {"dateTime": "2026-06-22T09:00:00+00:00"},
          "end": {"dateTime": "2026-06-22T09:30:00+00:00"}}
    row = {"id": "row1", "title": "Cremind Standup", "action": "do x",
           "schedule_kind": "recurrence", "rrule": "FREQ=DAILY",
           "status": "active", "source": "agent", "conversation_id": "c1"}
    occ = P._google_event_to_occurrence(ev, row)
    assert occ["subscription_id"] == "row1"
    assert occ["title"] == "Cremind Standup"
    assert occ["action"] == "do x"
    assert occ["is_recurring"] is True


def test_occurrence_no_match_is_readonly_google():
    ev = {"id": "g2", "summary": "Lunch",
          "start": {"dateTime": "2026-06-22T12:00:00+00:00"},
          "end": {"dateTime": "2026-06-22T13:00:00+00:00"}}
    occ = P._google_event_to_occurrence(ev, None)
    assert occ["subscription_id"] is None
    assert occ["source"] == "google"
    assert occ["read_only"] is True
    assert occ["title"] == "Lunch"


# ── list_occurrences reconcile ──────────────────────────────────────────────

class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def list_by_profile(self, profile):
        return list(self._rows)


def _provider_with(rows):
    gp = P.GoogleCalendarProvider("p")
    fake = _FakeStore(rows)
    gp._store = fake
    gp._internal._store = fake  # used by the internal fallback path
    return gp


def test_list_occurrences_reconciles_mirrored_and_pure(monkeypatch):
    rows = [{
        "id": "row1", "external_event_id": "gbase", "title": "Cremind Mtg", "action": "ping",
        "schedule_kind": "recurrence", "rrule": "FREQ=DAILY",
        "status": "active", "source": "agent", "conversation_id": "c1",
    }]
    gp = _provider_with(rows)
    events = {"items": [
        {"id": "gbase_20260622", "recurringEventId": "gbase", "summary": "Cremind Mtg",
         "start": {"dateTime": "2026-06-22T09:00:00+00:00"},
         "end": {"dateTime": "2026-06-22T09:30:00+00:00"}},
        {"id": "other", "summary": "Lunch",
         "start": {"dateTime": "2026-06-22T12:00:00+00:00"},
         "end": {"dateTime": "2026-06-22T13:00:00+00:00"}},
    ]}
    monkeypatch.setattr(gp, "_request", lambda method, path, **kw: events)

    occ = gp.list_occurrences("p", "2026-06-22T00:00:00", "2026-06-22T23:59:59")
    assert len(occ) == 2
    cremind = [o for o in occ if o["subscription_id"] == "row1"]
    assert len(cremind) == 1 and cremind[0]["action"] == "ping"
    pure = [o for o in occ if o["subscription_id"] is None]
    assert len(pure) == 1 and pure[0]["title"] == "Lunch" and pure[0]["source"] == "google"


def test_list_occurrences_falls_back_to_internal_on_api_error(monkeypatch):
    gp = _provider_with([])  # empty internal store

    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(gp, "_request", boom)
    occ = gp.list_occurrences("p", "2026-06-22T00:00:00", "2026-06-22T23:59:59")
    assert occ == []  # internal fallback over an empty store


# ── route presence ──────────────────────────────────────────────────────────

def test_google_routes_registered():
    from app.api.calendar import get_calendar_routes
    paths = {r.path for r in get_calendar_routes(None)}
    assert "/api/calendar/google/connect" in paths
    assert "/api/calendar/google/disconnect" in paths
