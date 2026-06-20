"""Unit tests for the ``datetime_parser`` built-in tool.

Simulate the structured arguments the routing LLM would emit (the tool's
``parameters`` schema) and assert the ``structured_content`` observation. Time
is pinned via the ``_now`` override so results are deterministic; ``now`` =
``2026-06-20T14:30:00`` (a Saturday).
"""

from __future__ import annotations

import asyncio

NOW = "2026-06-20T14:30:00"  # Saturday


def _run(arguments):
    from app.tools.builtin.datetime_parser import DatetimeParserTool
    tool = DatetimeParserTool()
    return asyncio.run(tool.run(arguments)).structured_content


def _elem(mode, time_range, offset_unit, offset_value):
    return {
        "mode": mode,
        "time_range": time_range,
        "offset_unit": offset_unit,
        "offset_value": offset_value,
    }


def test_single_instant_tomorrow():
    out = _run({
        "reasoning": "tomorrow",
        "parsable": True,
        "single_time_mode": True,
        "time_elements": [_elem("relative", "start", "day", 1)],
        "components_count": 1,
        "_now": NOW,
    })
    assert out == {"parsable": True, "time_single": {"datetime": "2026-06-21T14:30:00"}}


def test_today_expands_to_range_when_single_time_mode_false():
    out = _run({
        "reasoning": "today, whole day",
        "parsable": True,
        "single_time_mode": False,
        "time_elements": [_elem("relative", "start", "day", 0)],
        "components_count": 1,
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "time_range": {
            "start_date": {"datetime": "2026-06-20T00:00:00"},
            "end_date": {"datetime": "2026-06-20T23:59:59"},
        },
    }


def test_explicit_range_ignores_single_time_mode():
    # "from 2pm to 4pm" — two elements tagged start/end; single_time_mode absent
    # (defaults True) but is moot for an explicit range.
    out = _run({
        "reasoning": "from 2pm to 4pm",
        "parsable": True,
        "time_elements": [
            _elem("absolute", "start", "hour", 14),
            _elem("absolute", "end", "hour", 16),
        ],
        "components_count": 2,
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "time_range": {
            "start_date": {"datetime": "2026-06-20T14:00:00"},
            "end_date": {"datetime": "2026-06-20T16:00:00"},
        },
    }


def test_weekday_next_monday():
    out = _run({
        "reasoning": "next Monday",
        "parsable": True,
        "single_time_mode": True,
        # weekday element: mode is present (schema requires it) but ignored.
        "time_elements": [_elem("relative", "start", "monday", 1)],
        "components_count": 1,
        "_now": NOW,
    })
    assert out == {"parsable": True, "time_single": {"datetime": "2026-06-22T14:30:00"}}


def test_not_parsable_echoes_reasoning():
    out = _run({
        "reasoning": "No time mentioned",
        "parsable": False,
        "time_elements": [],
        "components_count": 0,
        "_now": NOW,
    })
    assert out == {"parsable": False, "reason": "No time mentioned"}


def test_date_inheritance_for_end_endpoint():
    # "next Friday from 10am to 12pm": the end endpoint only states the hour, so
    # it inherits next Friday's date (2026-06-26) from the start group.
    out = _run({
        "reasoning": "next Friday 10am-12pm",
        "parsable": True,
        "time_elements": [
            _elem("relative", "start", "friday", 1),
            _elem("absolute", "start", "hour", 10),
            _elem("absolute", "end", "hour", 12),
        ],
        "components_count": 3,
        "_now": NOW,
    })
    assert out == {
        "parsable": True,
        "time_range": {
            "start_date": {"datetime": "2026-06-26T10:00:00"},
            "end_date": {"datetime": "2026-06-26T12:00:00"},
        },
    }


def test_single_time_mode_defaults_true_when_omitted():
    out = _run({
        "reasoning": "in two hours",
        "parsable": True,
        "time_elements": [_elem("relative", "start", "hour", 2)],
        "components_count": 1,
        "_now": NOW,
    })
    assert out == {"parsable": True, "time_single": {"datetime": "2026-06-20T16:30:00"}}


def test_falls_back_to_real_now_without_override():
    # No _now: the tool uses datetime.now(); assert shape only, not the value.
    out = _run({
        "reasoning": "in one hour",
        "parsable": True,
        "single_time_mode": True,
        "time_elements": [_elem("relative", "start", "hour", 1)],
        "components_count": 1,
    })
    assert out["parsable"] is True
    assert "datetime" in out["time_single"]
