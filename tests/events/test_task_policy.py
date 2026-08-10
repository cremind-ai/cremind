"""The one place that decides what counts as an event task.

The load-bearing invariant: everything ``is_task_schedule_args`` approves at
dispatch really does get stamped as a task by ``schedule_kind_for``. If those
two ever disagree in that direction, a continuation turn could register an
event that silently never reports back — the conversation would wait forever.
"""

from __future__ import annotations

import time

import pytest

from app.events.task_policy import (
    TASK_TIMEOUT_DEFAULT_MINUTES,
    TASK_TIMEOUT_MAX_MINUTES,
    is_task_registration,
    is_task_schedule_args,
    is_task_subscribe_args,
    is_task_watcher_args,
    resolve_task_timeout,
    schedule_kind_for,
)


# ── timeouts ────────────────────────────────────────────────────────────────


def test_a_task_always_gets_a_deadline():
    """No timeout means the default, never "wait forever" by accident."""
    timeout_at, err = resolve_task_timeout(None, task=True)
    assert err is None
    assert timeout_at == pytest.approx(
        time.time() + TASK_TIMEOUT_DEFAULT_MINUTES * 60, abs=5,
    )


def test_explicit_minutes_are_honoured():
    timeout_at, err = resolve_task_timeout(90, task=True)
    assert err is None
    assert timeout_at == pytest.approx(time.time() + 5400, abs=5)


def test_standing_registration_has_no_deadline():
    assert resolve_task_timeout(None, task=False) == (None, None)


def test_timeout_without_task_is_a_correctable_error():
    timeout_at, err = resolve_task_timeout(30, task=False)
    assert timeout_at is None
    assert "only applies to a one-shot task" in err
    assert "Nothing was registered" in err


def test_out_of_range_and_junk_timeouts_are_rejected():
    for bad in (0, -5, TASK_TIMEOUT_MAX_MINUTES + 1, "soon", 1.5e9):
        timeout_at, err = resolve_task_timeout(bad, task=True)
        assert timeout_at is None, bad
        assert "must be a whole number of minutes" in err


# ── which calls create tasks ────────────────────────────────────────────────


def test_subscribe_and_watcher_task_flags():
    assert is_task_subscribe_args({"task": True}) is True
    assert is_task_subscribe_args({"trigger": ["x"]}) is False
    assert is_task_subscribe_args(None) is False
    assert is_task_watcher_args({"task": True}) is True
    assert is_task_watcher_args({}) is False


def test_a_plain_one_time_schedule_is_a_task():
    assert is_task_schedule_args({"dtstart": "2026-08-10T16:00:00"}) is True
    assert schedule_kind_for(rrule=None, all_day=False, duration_minutes=30) == "instant"


def test_recurring_and_calendar_block_schedules_are_not_tasks():
    # Recurring: fires forever, never reports back.
    assert is_task_schedule_args({"rrule": "FREQ=DAILY"}) is False
    assert schedule_kind_for(
        rrule="FREQ=DAILY", all_day=False, duration_minutes=30,
    ) == "recurrence"
    # A trip or a leave day is a calendar block, not an awaited outcome.
    assert is_task_schedule_args({"all_day": True}) is False
    assert is_task_schedule_args({"duration_minutes": 120}) is False
    assert is_task_schedule_args({"end": "2026-08-10T18:00:00"}) is False
    assert schedule_kind_for(
        rrule=None, all_day=True, duration_minutes=1440,
    ) == "interval"
    assert schedule_kind_for(
        rrule=None, all_day=False, duration_minutes=120,
    ) == "interval"


def test_dispatch_approval_implies_the_row_really_becomes_a_task():
    """The safety direction: approved-at-dispatch ⟹ actually delivers.

    ``is_task_schedule_args`` sees only raw model arguments; ``schedule_kind_for``
    sees the tool's normalized values. The approximation may be stricter (a
    harmless retry) but must never be looser (a conversation waiting on nothing).
    """
    candidates = [
        {},
        {"dtstart": "2026-08-10T16:00:00"},
        {"duration_minutes": 15},
        {"duration_minutes": 30},
        {"duration_minutes": 31},
        {"duration_minutes": 120},
        {"all_day": True},
        {"end": "2026-08-10T18:00:00"},
        {"rrule": "FREQ=WEEKLY"},
        {"rrule": "FREQ=DAILY", "duration_minutes": 10},
    ]
    for args in candidates:
        if not is_task_schedule_args(args):
            continue
        duration = int(args.get("duration_minutes") or 0) or 30
        kind = schedule_kind_for(
            rrule=args.get("rrule"),
            all_day=bool(args.get("all_day")),
            duration_minutes=duration,
        )
        assert kind == "instant", args


def test_is_task_registration_routes_by_leaf():
    assert is_task_registration("system_file", "register_file_watcher", {"task": True})
    assert not is_task_registration("system_file", "register_file_watcher", {})
    assert is_task_registration("scheduler", "schedule_create", {"dtstart": "x"})
    assert not is_task_registration("scheduler", "schedule_create", {"rrule": "FREQ=DAILY"})
    # Anything that isn't a registration leaf is simply not a task registration.
    assert not is_task_registration("system_file", "write_file", {"task": True})
