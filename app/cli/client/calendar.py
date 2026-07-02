"""Calendar & Schedule endpoints — `/api/calendar/*`, `/api/schedule-events/*`.

Thin async wrappers over the Calendar & Schedule API: the per-profile feature
switch, calendar occurrences, manual event CRUD, Google Calendar connect/
disconnect, and the raw schedule-event subscriptions (list / status).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote


async def get_settings(client) -> dict[str, Any]:
    resp = await client.get_json("/api/calendar/settings")
    return resp if isinstance(resp, dict) else {}


async def set_enabled(client, enabled: bool) -> dict[str, Any]:
    resp = await client.put_json("/api/calendar/settings", {"enabled": enabled})
    return resp if isinstance(resp, dict) else {}


async def list_events(
    client,
    *,
    range_from: Optional[str] = None,
    range_to: Optional[str] = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if range_from:
        params["from"] = range_from
    if range_to:
        params["to"] = range_to
    resp = await client.get_json("/api/calendar/events", params=params or None)
    return resp if isinstance(resp, dict) else {}


async def create_event(client, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post_json("/api/calendar/events", body)
    if isinstance(resp, dict) and isinstance(resp.get("event"), dict):
        return resp["event"]
    return {}


async def update_event(client, event_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    resp = await client.patch_json(f"/api/calendar/events/{quote(event_id, safe='')}", fields)
    if isinstance(resp, dict) and isinstance(resp.get("event"), dict):
        return resp["event"]
    return {}


async def delete_event(client, event_id: str) -> None:
    await client.delete(f"/api/calendar/events/{quote(event_id, safe='')}")


async def google_connect(client) -> str:
    """Returns the Google authorize URL (the caller opens it in a browser)."""
    resp = await client.post_json("/api/calendar/google/connect")
    if isinstance(resp, dict):
        return str(resp.get("authorize_url") or "")
    return ""


async def google_disconnect(client) -> None:
    await client.post_json("/api/calendar/google/disconnect")


async def list_subscriptions(client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/schedule-events")
    if isinstance(resp, dict) and isinstance(resp.get("subscriptions"), list):
        return [s for s in resp["subscriptions"] if isinstance(s, dict)]
    return []


async def set_status(client, event_id: str, status: str) -> dict[str, Any]:
    resp = await client.post_json(f"/api/schedule-events/{quote(event_id, safe='')}/status", {"status": status})
    if isinstance(resp, dict) and isinstance(resp.get("event"), dict):
        return resp["event"]
    return {}
