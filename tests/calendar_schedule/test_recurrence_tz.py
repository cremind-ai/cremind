"""The naive<->epoch bridge in ``app.calendar.recurrence`` must interpret naive
wall-clock in an explicit timezone, so a schedule fires at the configured local
time regardless of the process OS zone (UTC on a Docker/VPS install).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import app.calendar.recurrence as R


def test_to_epoch_interprets_naive_in_given_zone():
    # 2026-07-22 09:00 in New York (EDT, UTC-4) == 13:00 UTC.
    dt = datetime(2026, 7, 22, 9, 0, 0)
    epoch = R.to_epoch(dt, ZoneInfo("America/New_York"))
    expected = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc).timestamp()
    assert epoch == expected


def test_to_epoch_zone_changes_result():
    dt = datetime(2026, 7, 22, 9, 0, 0)
    ny = R.to_epoch(dt, ZoneInfo("America/New_York"))
    tokyo = R.to_epoch(dt, ZoneInfo("Asia/Tokyo"))
    # Same wall-clock, different zones -> different absolute instants.
    assert ny != tokyo


def test_from_epoch_returns_naive_walltime_in_zone():
    dt = datetime(2026, 7, 22, 9, 0, 0)
    tz = ZoneInfo("Asia/Tokyo")
    epoch = R.to_epoch(dt, tz)
    back = R.from_epoch(epoch, tz)
    assert back.tzinfo is None      # naive wall-clock
    assert back == dt               # round-trips to the same wall-clock


def test_round_trip_across_dst_zone():
    # A winter (EST, UTC-5) wall-clock round-trips too.
    dt = datetime(2026, 1, 15, 9, 0, 0)
    tz = ZoneInfo("America/New_York")
    assert R.from_epoch(R.to_epoch(dt, tz), tz) == dt


def test_default_tz_is_backward_compatible():
    # With no tz, the bridge uses the OS-local zone (unchanged legacy behavior):
    # to_epoch then from_epoch must round-trip the wall-clock.
    dt = datetime(2026, 7, 22, 9, 30, 0)
    assert R.from_epoch(R.to_epoch(dt)) == dt
