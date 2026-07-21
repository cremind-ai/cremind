"""Tests for GoogleCalendarProvider mapping/mirror/reconcile + route presence.

The Calendar REST + token are mocked; the internal trigger store is faked, so
these are pure-logic tests (no DB, no network, no Google)."""

from __future__ import annotations

import types
from zoneinfo import ZoneInfo

import app.calendar.provider as P

# The mirror helpers now take an explicit zone (was the OS-local zone). These
# pure-logic tests pin UTC so naive<->offset conversions are deterministic.
_UTC = ZoneInfo("UTC")


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_event_body_recurring():
    body = P._google_event_body(
        title="Standup", dtstart="2026-06-22T09:00:00", duration_minutes=30,
        rrule="FREQ=DAILY", action="check email", tz=_UTC,
    )
    assert body["summary"] == "Standup"
    assert body["recurrence"] == ["RRULE:FREQ=DAILY"]
    assert "dateTime" in body["start"] and "dateTime" in body["end"]
    assert "Cremind action: check email" in body["description"]


def test_event_body_no_action_no_recurrence():
    body = P._google_event_body(
        title="Dentist", dtstart="2026-06-22T15:00:00", duration_minutes=60,
        rrule=None, action="", tz=_UTC,
    )
    assert "recurrence" not in body
    assert "Cremind event" in body["description"]


def test_event_body_with_action_notes_command():
    body = P._google_event_body(
        title="Standup", dtstart="2026-06-22T09:00:00", duration_minutes=30,
        rrule=None, action="join the call", tz=_UTC,
    )
    assert "Cremind action: join the call" in body["description"]


def test_google_dt_to_local_iso_allday():
    assert P._google_dt_to_local_iso({"date": "2026-06-22"}, _UTC) == "2026-06-22T00:00:00"


def test_event_body_all_day_uses_date_range():
    # 3-day trip = 3*1440 minutes, midnight start -> Google date-only, end EXCLUSIVE.
    body = P._google_event_body(
        title="Trip", dtstart="2026-06-22T00:00:00", duration_minutes=3 * 1440,
        rrule=None, action="", all_day=True, tz=_UTC,
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
    occ = P._google_event_to_occurrence(ev, row, _UTC)
    assert occ["subscription_id"] == "row1"
    assert occ["title"] == "Cremind Standup"
    assert occ["action"] == "do x"
    assert occ["is_recurring"] is True


def test_occurrence_no_match_is_readonly_google():
    ev = {"id": "g2", "summary": "Lunch",
          "start": {"dateTime": "2026-06-22T12:00:00+00:00"},
          "end": {"dateTime": "2026-06-22T13:00:00+00:00"}}
    occ = P._google_event_to_occurrence(ev, None, _UTC)
    assert occ["subscription_id"] is None
    assert occ["source"] == "google"
    assert occ["read_only"] is True
    assert occ["title"] == "Lunch"


# ── list_occurrences reconcile ──────────────────────────────────────────────

class _FakeStore:
    def __init__(self, rows):
        self._rows = rows
        self.inserted = None

    def list_by_profile(self, profile):
        return list(self._rows)

    def insert(self, **kwargs):
        row = {"id": "newrow", **kwargs}
        self.inserted = row
        return row

    def update_fields(self, event_id, **fields):
        return {"id": event_id, **fields}

    def get(self, event_id):
        return None


def _provider_with(rows):
    gp = P.GoogleCalendarProvider("p")
    fake = _FakeStore(rows)
    gp._store = fake
    gp._internal._store = fake  # used by the internal fallback / merge path
    # Trigger engine is irrelevant to these pure-logic tests.
    gp._internal._manager = lambda: types.SimpleNamespace(
        arm=lambda row: None, refresh=lambda x: None, remove=lambda x: None,
    )
    return gp


# ── google_supports_rrule ────────────────────────────────────────────────────

def test_google_supports_rrule():
    assert P.google_supports_rrule(None) is True
    assert P.google_supports_rrule("FREQ=DAILY") is True
    assert P.google_supports_rrule("FREQ=WEEKLY;BYDAY=MO") is True
    assert P.google_supports_rrule("FREQ=MONTHLY;BYMONTHDAY=15") is True
    assert P.google_supports_rrule("FREQ=HOURLY;INTERVAL=2") is False
    assert P.google_supports_rrule("freq=minutely") is False  # case-insensitive
    assert P.google_supports_rrule("FREQ=SECONDLY") is False


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


def test_list_occurrences_merges_local_only_events(monkeypatch):
    # A mirrored row (external_event_id set) is represented by its Google item; a
    # local-only row (never mirrored) must ALSO show while connected — otherwise
    # locally-created reminders vanish from the page the moment Google connects.
    rows = [
        {  # mirrored -> comes from Google, must not be double-counted
            "id": "row1", "external_event_id": "gbase", "title": "Mirrored",
            "schedule_kind": "instant", "rrule": None, "status": "active",
            "dtstart": "2026-06-22T09:00:00", "duration_minutes": 30,
        },
        {  # local-only -> merged in from the internal store
            "id": "row2", "external_event_id": None, "title": "Local reminder",
            "action": "take meds", "schedule_kind": "instant", "rrule": None,
            "status": "active", "source": "agent", "conversation_id": "c1",
            "dtstart": "2026-06-22T10:00:00", "duration_minutes": 30,
        },
    ]
    gp = _provider_with(rows)
    events = {"items": [
        {"id": "gbase", "recurringEventId": "gbase", "summary": "Mirrored",
         "start": {"dateTime": "2026-06-22T09:00:00+00:00"},
         "end": {"dateTime": "2026-06-22T09:30:00+00:00"}},
    ]}
    monkeypatch.setattr(gp, "_request", lambda method, path, **kw: events)

    occ = gp.list_occurrences("p", "2026-06-22T00:00:00", "2026-06-22T23:59:59")
    assert sum(1 for o in occ if o["title"] == "Mirrored") == 1  # no duplicate
    local = [o for o in occ if o["title"] == "Local reminder"]
    assert len(local) == 1
    assert local[0]["subscription_id"] == "row2" and local[0]["action"] == "take meds"


def test_create_event_skips_google_mirror_for_subdaily(monkeypatch):
    gp = _provider_with([])

    def boom(*a, **k):
        raise AssertionError("must not mirror a sub-daily recurrence to Google")

    monkeypatch.setattr(gp, "_request", boom)
    row = gp.create_event(
        profile="p", conversation_id="c1", title="meds", action="take meds",
        schedule_kind="recurrence", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, rrule="FREQ=HOURLY;INTERVAL=2",
    )
    # Row created locally (fires + shows via merge), never mirrored to Google.
    assert row["rrule"] == "FREQ=HOURLY;INTERVAL=2"
    assert not row.get("external_event_id")


def test_create_event_mirrors_supported_recurrence(monkeypatch):
    gp = _provider_with([])
    seen = {}

    def fake_request(method, path, **kw):
        seen["method"] = method
        return {"id": "gnew"}

    monkeypatch.setattr(gp, "_request", fake_request)
    row = gp.create_event(
        profile="p", conversation_id="c1", title="standup", action="",
        schedule_kind="recurrence", dtstart="2026-06-22T09:00:00",
        duration_minutes=30, rrule="FREQ=DAILY",
    )
    assert seen.get("method") == "POST"
    assert row.get("external_event_id") == "gnew"


# ── route presence ──────────────────────────────────────────────────────────

def test_google_routes_registered():
    from app.api.calendar import get_calendar_routes
    paths = {r.path for r in get_calendar_routes(None)}
    assert "/api/calendar/google/connect" in paths
    assert "/api/calendar/google/disconnect" in paths
