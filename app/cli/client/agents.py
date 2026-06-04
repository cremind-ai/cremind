"""Agents endpoints — `/api/agents*`.

Mirrors `cli/internal/client/agents.go`. Each agent record is a loose dict
because the schema varies by agent_type (a2a vs mcp).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from app.cli.client._base import Client


async def list_agents(client: Client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/agents")
    if isinstance(resp, dict) and isinstance(resp.get("agents"), list):
        return [a for a in resp["agents"] if isinstance(a, dict)]
    return []


async def add_agent(client: Client, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post_json("/api/agents", body)
    if isinstance(resp, dict) and isinstance(resp.get("agent"), dict):
        return resp["agent"]
    return {}


async def remove_agent(client: Client, tool_id: str) -> None:
    await client.delete(f"/api/agents/{quote(tool_id, safe='')}")


async def set_agent_enabled(client: Client, tool_id: str, enabled: bool) -> None:
    await client.put_json(
        f"/api/agents/{quote(tool_id, safe='')}/enabled",
        {"enabled": enabled},
    )


async def reconnect_agent(client: Client, tool_id: str) -> None:
    await client.post_json(f"/api/agents/{quote(tool_id, safe='')}/reconnect")


async def get_agent_auth_url(
    client: Client,
    tool_id: str,
    return_url: str = "",
) -> str:
    path = f"/api/agents/{quote(tool_id, safe='')}/auth-url"
    if return_url:
        path += "?" + urlencode({"return_url": return_url})
    resp = await client.get_json(path)
    if isinstance(resp, dict):
        return str(resp.get("auth_url") or "")
    return ""


async def unlink_agent(client: Client, tool_id: str) -> None:
    await client.post_json(f"/api/agents/{quote(tool_id, safe='')}/unlink")


async def get_agent_config(client: Client, tool_id: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/agents/{quote(tool_id, safe='')}/config")
    if isinstance(resp, dict) and isinstance(resp.get("config"), dict):
        return resp["config"]
    return {}


async def update_agent_config(
    client: Client,
    tool_id: str,
    body: dict[str, Any],
) -> None:
    """Partial update — only keys present in `body` are written."""
    await client.put_json(f"/api/agents/{quote(tool_id, safe='')}/config", body)
