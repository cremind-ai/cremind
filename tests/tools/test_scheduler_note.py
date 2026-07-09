"""The ``scheduler`` parser attaches an anti-false-completion note to booking
results (nothing is registered until ``schedule_create`` runs). When the
Calendar & Schedule feature is ON the note names ``schedule_create``; when OFF it
just says "parsed only" (never pointing at a tool the model can't see). Query
kinds (window/constraint) get no note.
"""

from __future__ import annotations

import asyncio

import app.tools.builtin.scheduler as sched

NOW = "2026-06-20T14:30:00"  # Saturday


def _run(arguments):
    tool = sched.SchedulerTool()
    return asyncio.run(tool.run(arguments)).structured_content


def _elem(mode, time_range, offset_unit, offset_value):
    return {
        "mode": mode,
        "time_range": time_range,
        "offset_unit": offset_unit,
        "offset_value": offset_value,
    }


def _recurrence_args(profile=None):
    args = {
        "reasoning": "every 2 hours",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "hourly", "interval": 2},
        "_now": NOW,
    }
    if profile is not None:
        args["_profile"] = profile
    return args


def test_booking_note_names_schedule_create_when_feature_on(monkeypatch):
    # run() does `from app.calendar.feature import is_enabled` at call time, so
    # patching the source module is what takes effect.
    import app.calendar.feature as feature
    monkeypatch.setattr(feature, "is_enabled", lambda profile: True)

    out = _run(_recurrence_args(profile="admin"))
    note = out["registration_note"]
    assert "schedule_create" in note
    assert "nothing is scheduled yet" in note.lower()


def test_booking_note_omits_schedule_create_when_feature_off(monkeypatch):
    import app.calendar.feature as feature
    monkeypatch.setattr(feature, "is_enabled", lambda profile: False)

    out = _run(_recurrence_args(profile="admin"))
    note = out["registration_note"]
    assert "schedule_create" not in note
    assert "nothing is scheduled yet" in note.lower()


def test_booking_note_present_without_profile():
    # No _profile → feature reads as off → the feature-off note (no schedule_create).
    out = _run(_recurrence_args(profile=None))
    assert "registration_note" in out
    assert "schedule_create" not in out["registration_note"]


def test_query_kinds_get_no_note():
    # A window is a lookup, not a booking — no registration note.
    out = _run({
        "reasoning": "my availability this week",
        "parsable": True,
        "schedule_kind": "window",
        "single_time_mode": False,
        "time_elements": [_elem("relative", "start", "day", 0)],
        "components_count": 1,
        "_now": NOW,
    })
    assert "registration_note" not in out


def test_unparsable_gets_no_note():
    out = _run({
        "reasoning": "no schedule here",
        "parsable": False,
        "schedule_kind": "instant",
        "_now": NOW,
    })
    assert "registration_note" not in out
