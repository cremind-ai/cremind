"""Skill-events endpoints — `/api/skill-events*`, `/api/skills/*/listener-*`.

Mirrors `cli/internal/client/skillevents.go`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def list_skill_event_subscriptions(client: Client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/skill-events")
    if isinstance(resp, dict) and isinstance(resp.get("subscriptions"), list):
        return [s for s in resp["subscriptions"] if isinstance(s, dict)]
    return []


async def delete_skill_event_subscription(client: Client, sub_id: str) -> None:
    await client.delete(f"/api/skill-events/{quote(sub_id, safe='')}")


async def simulate_skill_event(
    client: Client,
    sub_id: str,
    content: str,
    filename: str = "",
) -> None:
    body: dict[str, str] = {"content": content}
    if filename:
        body["filename"] = filename
    await client.post_json(
        f"/api/skill-events/{quote(sub_id, safe='')}/simulate",
        body,
    )


async def list_skill_events(client: Client, skill: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/skills/{quote(skill, safe='')}/events")
    return resp if isinstance(resp, dict) else {}


async def get_listener_status(client: Client, skill: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/skills/{quote(skill, safe='')}/listener-status")
    return resp if isinstance(resp, dict) else {}


async def start_listener(client: Client, skill: str) -> dict[str, Any]:
    resp = await client.post_json(f"/api/skills/{quote(skill, safe='')}/listener-start")
    return resp if isinstance(resp, dict) else {}


def skill_events_admin_stream_path() -> str:
    return "/api/skill-events/admin/stream"


def skill_event_notifications_stream_path(since: int = 0) -> str:
    if since > 0:
        return f"/api/skill-events/notifications/stream?since={since}"
    return "/api/skill-events/notifications/stream"
