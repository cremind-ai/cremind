"""Unit tests for the ``current_time`` built-in tool.

Time is pinned via the ``_now`` override (an aware ISO string) so timezone
conversions are deterministic regardless of the test host's clock/zone.
"""

from __future__ import annotations

import asyncio


def _run(arguments):
    from app.tools.builtin.current_time import GetCurrentTimeTool
    tool = GetCurrentTimeTool()
    return asyncio.run(tool.run(arguments)).structured_content


def test_specific_timezone_tokyo():
    # 00:00 UTC is 09:00 in Tokyo (no DST → always +09:00).
    out = _run({"timezone": "Asia/Tokyo", "_now": "2026-06-20T00:00:00+00:00"})
    assert out["timezone"] == "Asia/Tokyo"
    assert out["utc_offset"] == "+09:00"
    assert out["iso"].startswith("2026-06-20T09:00:00")


def test_specific_timezone_negative_offset():
    # 12:00 UTC is 08:00 in New York in June (EDT → -04:00).
    out = _run({"timezone": "America/New_York", "_now": "2026-06-20T12:00:00+00:00"})
    assert out["timezone"] == "America/New_York"
    assert out["utc_offset"] == "-04:00"
    assert out["iso"].startswith("2026-06-20T08:00:00")


def test_invalid_timezone_returns_structured_error():
    out = _run({"timezone": "Mars/Olympus"})
    assert out["error"] == "InvalidTimezone"
    assert out["requested"] == "Mars/Olympus"
    assert "iso" not in out  # no time is reported for a bad zone


def test_no_timezone_returns_local_time_shape():
    out = _run({})
    # Server-local time: assert the observation shape, not a host-dependent value.
    assert set(out) == {"iso", "human_readable", "timezone", "utc_offset"}
    assert isinstance(out["iso"], str) and "T" in out["iso"]
    assert isinstance(out["human_readable"], str) and out["human_readable"]
