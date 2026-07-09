"""Unit tests for the ``scheduler`` built-in tool (schedule parser).

Simulate the structured arguments the routing LLM would emit (the tool's
``parameters`` schema) and assert the ``structured_content`` observation. Time
is pinned via the ``_now`` override so results are deterministic; ``now`` =
``2026-06-20T14:30:00`` (a Saturday).
"""

from __future__ import annotations

import asyncio

NOW = "2026-06-20T14:30:00"  # Saturday


def _run(arguments):
    from app.tools.builtin.scheduler import SchedulerTool
    tool = SchedulerTool()
    out = asyncio.run(tool.run(arguments)).structured_content
    # The anti-false-completion note is orthogonal to parse structure and is
    # exercised on its own in test_scheduler_note.py; strip it here so these
    # structural assertions stay focused.
    if isinstance(out, dict):
        out.pop("registration_note", None)
    return out


def _elem(mode, time_range, offset_unit, offset_value):
    return {
        "mode": mode,
        "time_range": time_range,
        "offset_unit": offset_unit,
        "offset_value": offset_value,
    }


# ── instant ────────────────────────────────────────────────────────────────


def test_instant_tomorrow_at_9():
    out = _run({
        "reasoning": "single point tomorrow at 9am",
        "parsable": True,
        "schedule_kind": "instant",
        "single_time_mode": True,
        "time_elements": [
            _elem("relative", "start", "day", 1),
            _elem("absolute", "start", "hour", 9),
        ],
        "components_count": 2,
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "schedule_kind": "instant",
        "intent": "book",
        "timezone": "pending",
        "instant": {"datetime": "2026-06-21T09:00:00"},
        "default_duration_minutes": 30,
        "constraints": [],
    }


# ── interval ───────────────────────────────────────────────────────────────


def test_interval_explicit_end():
    out = _run({
        "reasoning": "from 2pm to 4pm today",
        "parsable": True,
        "schedule_kind": "interval",
        "time_elements": [
            _elem("absolute", "start", "hour", 14),
            _elem("absolute", "end", "hour", 16),
        ],
        "components_count": 2,
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "schedule_kind": "interval",
        "intent": "book",
        "timezone": "pending",
        "interval": {
            "start": "2026-06-20T14:00:00",
            "end": "2026-06-20T16:00:00",
            "duration_minutes": 120,
        },
        "constraints": [],
    }


def test_interval_via_duration():
    out = _run({
        "reasoning": "90-minute meeting starting at 1pm",
        "parsable": True,
        "schedule_kind": "interval",
        "time_elements": [_elem("absolute", "start", "hour", 13)],
        "components_count": 1,
        "duration": {"hours": 1, "minutes": 30},
        "_now": NOW,
    })
    assert out["interval"] == {
        "start": "2026-06-20T13:00:00",
        "end": "2026-06-20T14:30:00",
        "duration_minutes": 90,
    }


# ── window ─────────────────────────────────────────────────────────────────


def test_window_today_expands_to_full_day():
    out = _run({
        "reasoning": "availability today (query region)",
        "parsable": True,
        "schedule_kind": "window",
        "single_time_mode": False,
        "time_elements": [_elem("relative", "start", "day", 0)],
        "components_count": 1,
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "schedule_kind": "window",
        "intent": "query",
        "timezone": "pending",
        "window": {
            "range_start": "2026-06-20T00:00:00",
            "range_end": "2026-06-20T23:59:59",
        },
        "constraints": [],
    }


# ── recurrence ─────────────────────────────────────────────────────────────


def test_recurrence_every_weekday_at_9_with_preview():
    out = _run({
        "reasoning": "every weekday at 9 — weekly recurrence on Mon-Fri",
        "parsable": True,
        "schedule_kind": "recurrence",
        "single_time_mode": True,
        "time_elements": [_elem("absolute", "start", "hour", 9)],
        "components_count": 1,
        "recurrence": {
            "frequency": "weekly",
            "by_weekday": ["MO", "TU", "WE", "TH", "FR"],
        },
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "schedule_kind": "recurrence",
        "intent": "book",
        "timezone": "pending",
        "recurrence": {
            # DTSTART snapped to the first matching occurrence (Monday) since the
            # anchor (Saturday) does not match the rule.
            "dtstart": "2026-06-22T09:00:00",
            "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
            "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"],
            "recurrence_end": {"type": "never"},
            "duration_minutes": 30,
            "preview": [
                "2026-06-22T09:00:00",
                "2026-06-23T09:00:00",
                "2026-06-24T09:00:00",
                "2026-06-25T09:00:00",
                "2026-06-26T09:00:00",
            ],
        },
        "constraints": [],
    }


def test_recurrence_every_other_monday():
    out = _run({
        "reasoning": "every other Monday",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "weekly", "interval": 2, "by_weekday": ["MO"]},
        "_now": NOW,
    })
    assert out["recurrence"]["rrule"] == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"


def test_recurrence_count_no_preview_for_monthly():
    out = _run({
        "reasoning": "the 15th of every month for 6 months",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "monthly", "by_monthday": [15], "count": 6},
        "_now": NOW,
    })
    rec = out["recurrence"]
    assert rec["rrule"] == "FREQ=MONTHLY;BYMONTHDAY=15;COUNT=6"
    assert rec["recurrence_end"] == {"type": "count", "value": 6}
    assert "dtstart" not in rec  # no anchor given
    assert "preview" not in rec  # monthly: no preview


def test_recurrence_subdaily_anchors_to_now():
    # "every 2 hours" has no time-of-day anchor and no bounded preview. dtstart
    # must be anchored to now (rounded to the minute) so the grid starts ~now,
    # rather than left unset for the LLM to invent (and potentially forward stale).
    out = _run({
        "reasoning": "every 2 hours",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "hourly", "interval": 2},
        "_now": NOW,
    })
    rec = out["recurrence"]
    assert rec["rrule"] == "FREQ=HOURLY;INTERVAL=2"
    assert rec["dtstart"] == "2026-06-20T14:30:00"  # now, rounded to the minute


def test_recurrence_ordinal_first_monday():
    out = _run({
        "reasoning": "first Monday of each month",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "monthly", "by_weekday": ["MO"], "by_setpos": [1]},
        "_now": NOW,
    })
    assert out["recurrence"]["rrule"] == "FREQ=MONTHLY;BYDAY=1MO"


def test_recurrence_until_kept_out_of_rrule_string():
    out = _run({
        "reasoning": "every day at 9 until next Friday",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [_elem("absolute", "start", "hour", 9)],
        "components_count": 1,
        "recurrence": {"frequency": "daily"},
        "until_elements": [_elem("relative", "start", "friday", 1)],
        "_now": NOW,
    })
    rec = out["recurrence"]
    assert rec["rrule"] == "FREQ=DAILY"  # UNTIL not baked into the naive string
    assert rec["recurrence_end"] == {"type": "until", "value": "2026-06-26T14:30:00"}
    assert rec["dtstart"] == "2026-06-20T09:00:00"


# ── explicit_set ───────────────────────────────────────────────────────────


def test_explicit_set_shared_time():
    out = _run({
        "reasoning": "July 3 and July 5 at 2pm — two distinct dates",
        "parsable": True,
        "schedule_kind": "explicit_set",
        "single_time_mode": True,
        "time_elements": [_elem("absolute", "start", "hour", 14)],  # shared 2pm
        "components_count": 1,
        "members": [
            {"member_kind": "instant", "time_elements": [
                _elem("absolute", "start", "month", 7),
                _elem("absolute", "start", "day", 3),
            ]},
            {"member_kind": "instant", "time_elements": [
                _elem("absolute", "start", "month", 7),
                _elem("absolute", "start", "day", 5),
            ]},
        ],
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "schedule_kind": "explicit_set",
        "intent": "book",
        "timezone": "pending",
        "explicit_set": {
            "occurrences": [
                {"instant": "2026-07-03T14:00:00"},
                {"instant": "2026-07-05T14:00:00"},
            ]
        },
        "constraints": [],
    }


# ── constraint ─────────────────────────────────────────────────────────────


def test_standalone_constraint_weekday_afternoons():
    out = _run({
        "reasoning": "weekday afternoons only — pure filter",
        "parsable": True,
        "schedule_kind": "constraint",
        "time_elements": [],
        "components_count": 0,
        "constraints": [
            {"type": "weekday_membership", "weekdays": ["MO", "TU", "WE", "TH", "FR"]},
            {"type": "time_of_day_band", "band": "afternoon"},
        ],
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "schedule_kind": "constraint",
        "intent": "filter",
        "timezone": "pending",
        "constraints": [
            {
                "type": "weekday_membership",
                "weekdays": ["MO", "TU", "WE", "TH", "FR"],
                "weekday_indices": [0, 1, 2, 3, 4],
            },
            {
                "type": "time_of_day_band",
                "band": "afternoon",
                "start_time": "12:00:00",
                "end_time": "18:00:00",
            },
        ],
    }


def test_recurrence_with_cooccurring_constraints():
    out = _run({
        "reasoning": "every day in the afternoon, weekdays only",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "daily"},
        "constraints": [
            {"type": "time_of_day_band", "band": "afternoon"},
            {"type": "weekday_membership", "weekdays": ["MO", "TU", "WE", "TH", "FR"]},
        ],
        "_now": NOW,
    })
    assert out["schedule_kind"] == "recurrence"
    assert out["recurrence"]["rrule"] == "FREQ=DAILY"
    assert out["constraints"] == [
        {
            "type": "time_of_day_band",
            "band": "afternoon",
            "start_time": "12:00:00",
            "end_time": "18:00:00",
        },
        {
            "type": "weekday_membership",
            "weekdays": ["MO", "TU", "WE", "TH", "FR"],
            "weekday_indices": [0, 1, 2, 3, 4],
        },
    ]


def test_custom_band_explicit_hours():
    out = _run({
        "reasoning": "9 to 5",
        "parsable": True,
        "schedule_kind": "constraint",
        "constraints": [
            {"type": "time_of_day_band", "band": "custom", "start_hour": 9, "end_hour": 17},
        ],
        "_now": NOW,
    })
    assert out["constraints"][0] == {
        "type": "time_of_day_band",
        "band": "custom",
        "start_time": "09:00:00",
        "end_time": "17:00:00",
    }


# ── error / not-parsable paths ─────────────────────────────────────────────


def test_not_parsable_echoes_reasoning():
    out = _run({
        "reasoning": "No schedule mentioned",
        "parsable": False,
        "schedule_kind": "instant",
        "time_elements": [],
        "components_count": 0,
        "_now": NOW,
    })
    assert out == {"parsable": False, "reason": "No schedule mentioned"}


def test_constraint_kind_without_predicates_is_not_parsable():
    out = _run({
        "reasoning": "claimed a constraint but gave none",
        "parsable": True,
        "schedule_kind": "constraint",
        "constraints": [],
        "_now": NOW,
    })
    assert out["parsable"] is False


def test_bad_frequency_surfaces_conversion_error():
    out = _run({
        "reasoning": "fortnightly is not a valid FREQ",
        "parsable": True,
        "schedule_kind": "recurrence",
        "time_elements": [],
        "components_count": 0,
        "recurrence": {"frequency": "fortnightly"},
        "_now": NOW,
    })
    assert out["error"] == "ConversionError"
    assert out["parsable"] is False


def test_falls_back_to_real_now_without_override():
    # No _now: the tool uses datetime.now(); assert shape only, not the value.
    out = _run({
        "reasoning": "in one hour",
        "parsable": True,
        "schedule_kind": "instant",
        "single_time_mode": True,
        "time_elements": [_elem("relative", "start", "hour", 1)],
        "components_count": 1,
    })
    assert out["parsable"] is True
    assert "datetime" in out["instant"]
