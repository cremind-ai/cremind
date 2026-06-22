"""Unit tests for the datetime computation library (``app.utils.datetime``).

Deterministic: every test pins ``current_date_str`` to a fixed reference
``now = 2026-06-20T14:30:00`` (a Saturday), so the relative/weekday arithmetic
has a known answer. Pure functions, no I/O.
"""

from __future__ import annotations

import pytest

from app.utils.datetime import (
    AbsoluteTime,
    RelativeTime,
    WeekdayOffset,
    TimeSingle,
    TimeRange,
    TimeRangeDate,
    TimeInputPayload,
    convert_datetime_payload,
)


NOW = "2026-06-20T14:30:00"  # Saturday


def _single(**kwargs) -> TimeInputPayload:
    return TimeInputPayload(time_single=TimeSingle(**kwargs))


def _convert(payload: TimeInputPayload, single_time_mode: bool = True):
    return convert_datetime_payload(payload, NOW, single_time_mode)


# ── time_single, single_time_mode=True (one instant) ───────────────────────

def test_relative_tomorrow():
    res = _convert(_single(relative=RelativeTime(day=1)))
    assert res.parsable is True
    assert res.time_single.datetime == "2026-06-21T14:30:00"


def test_relative_yesterday():
    res = _convert(_single(relative=RelativeTime(day=-1)))
    assert res.time_single.datetime == "2026-06-19T14:30:00"


def test_relative_in_two_hours():
    res = _convert(_single(relative=RelativeTime(hour=2)))
    assert res.time_single.datetime == "2026-06-20T16:30:00"


def test_now_flag():
    res = _convert(_single(now=True))
    assert res.parsable is True
    assert res.time_single.now is True
    assert res.time_single.datetime is None


def test_absolute_3pm_forces_minute_zero():
    # "3pm" -> absolute hour 15; minute/second snap to 0.
    res = _convert(_single(absolute=AbsoluteTime(hour=15)))
    assert res.time_single.datetime == "2026-06-20T15:00:00"


def test_relative_day_zero_is_current_moment():
    # day offset 0 ("today") in single mode is the current instant, not a range.
    res = _convert(_single(relative=RelativeTime(day=0)))
    assert res.time_single.datetime == "2026-06-20T14:30:00"


# ── weekday occurrences ────────────────────────────────────────────────────

def test_weekday_next_monday():
    res = _convert(_single(weekday=WeekdayOffset(name="monday", offset=1)))
    assert res.time_single.datetime == "2026-06-22T14:30:00"


def test_weekday_this_saturday_is_today():
    res = _convert(_single(weekday=WeekdayOffset(name="saturday", offset=0)))
    assert res.time_single.datetime == "2026-06-20T14:30:00"


def test_weekday_last_friday():
    res = _convert(_single(weekday=WeekdayOffset(name="friday", offset=-1)))
    assert res.time_single.datetime == "2026-06-19T14:30:00"


# ── time_single, single_time_mode=False (expand to range) ──────────────────

def test_today_expands_to_full_day():
    res = _convert(_single(relative=RelativeTime(day=0)), single_time_mode=False)
    assert res.time_range["start_date"].datetime == "2026-06-20T00:00:00"
    assert res.time_range["end_date"].datetime == "2026-06-20T23:59:59"


def test_next_month_expands_to_month_range():
    res = _convert(_single(relative=RelativeTime(month=1)), single_time_mode=False)
    assert res.time_range["start_date"].datetime == "2026-07-01T00:00:00"
    assert res.time_range["end_date"].datetime == "2026-07-31T23:59:59"


def test_absolute_year_expands_to_year_range():
    res = _convert(_single(absolute=AbsoluteTime(year=2025)), single_time_mode=False)
    assert res.time_range["start_date"].datetime == "2025-01-01T00:00:00"
    assert res.time_range["end_date"].datetime == "2025-12-31T23:59:59"


def test_minute_precision_expands_to_that_minute():
    res = _convert(_single(absolute=AbsoluteTime(hour=9, minute=15)), single_time_mode=False)
    assert res.time_range["start_date"].datetime == "2026-06-20T09:15:00"
    assert res.time_range["end_date"].datetime == "2026-06-20T09:15:59"


def test_now_in_range_mode_is_now_on_both_ends():
    res = _convert(_single(now=True), single_time_mode=False)
    assert res.time_range["start_date"].now is True
    assert res.time_range["end_date"].now is True


# ── explicit time_range ────────────────────────────────────────────────────

def test_explicit_range_two_o_clock_to_four():
    payload = TimeInputPayload(
        time_range=TimeRange(
            start_date=TimeRangeDate(absolute=AbsoluteTime(hour=14)),
            end_date=TimeRangeDate(absolute=AbsoluteTime(hour=16)),
        )
    )
    res = convert_datetime_payload(payload, NOW)
    assert res.time_range["start_date"].datetime == "2026-06-20T14:00:00"
    assert res.time_range["end_date"].datetime == "2026-06-20T16:00:00"


# ── not parsable ───────────────────────────────────────────────────────────

def test_empty_payload_not_parsable():
    res = convert_datetime_payload(TimeInputPayload(), NOW)
    assert res.parsable is False
    assert "No datetime information" in res.reason


def test_empty_single_object_not_parsable():
    res = _convert(_single())
    assert res.parsable is False
    assert "empty" in res.reason


# ── known limitation: day-overflow on relative month shift raises ──────────

def test_day_overflow_relative_month_raises():
    # Jan 31 + 1 month -> "Feb 31", which datetime.replace rejects. This is a
    # faithful-port limitation; the tool wrapper turns it into a structured
    # error rather than a crash (see test_datetime_parser).
    payload = TimeInputPayload(time_single=TimeSingle(relative=RelativeTime(month=1)))
    with pytest.raises(ValueError):
        convert_datetime_payload(payload, "2026-01-31T12:00:00", True)
