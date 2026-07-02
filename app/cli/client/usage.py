"""Usage & cost endpoint — `/api/usage/summary`."""

from __future__ import annotations

from typing import Any, Optional


async def get_summary(
    client,
    *,
    start_ms: Optional[float] = None,
    end_ms: Optional[float] = None,
    tz_offset_min: int = 0,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if start_ms is not None:
        params["start"] = start_ms
    if end_ms is not None:
        params["end"] = end_ms
    if tz_offset_min:
        params["tz_offset"] = tz_offset_min
    if profile:
        params["profile"] = profile
    resp = await client.get_json("/api/usage/summary", params=params or None)
    return resp if isinstance(resp, dict) else {}
