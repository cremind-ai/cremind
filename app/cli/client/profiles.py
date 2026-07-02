"""Profile endpoints — `/api/profiles*`.

Mirrors `cli/internal/client/profiles.go`.
"""

from __future__ import annotations

from urllib.parse import quote

from app.cli.client._base import Client


async def list_profiles(client: Client) -> list[str]:
    resp = await client.get_json("/api/profiles")
    if isinstance(resp, dict):
        profiles = resp.get("profiles")
        if isinstance(profiles, list):
            return [str(p) for p in profiles]
    return []


async def create_profile(client: Client, name: str) -> None:
    await client.post_json("/api/profiles", {"name": name})


async def delete_profile(client: Client, name: str) -> None:
    await client.delete(f"/api/profiles/{quote(name, safe='')}")


async def get_persona(client: Client, name: str) -> str:
    resp = await client.get_json(f"/api/profiles/{quote(name, safe='')}/persona")
    if isinstance(resp, dict):
        return str(resp.get("content") or "")
    return ""


async def set_persona(client: Client, name: str, content: str) -> None:
    await client.put_json(
        f"/api/profiles/{quote(name, safe='')}/persona",
        {"content": content},
    )


async def get_agent_name(client: Client, name: str) -> str:
    resp = await client.get_json(f"/api/profiles/{quote(name, safe='')}/agent-name")
    if isinstance(resp, dict):
        return str(resp.get("name") or "")
    return ""


async def set_agent_name(client: Client, name: str, agent_name: str) -> None:
    await client.put_json(
        f"/api/profiles/{quote(name, safe='')}/agent-name",
        {"name": agent_name},
    )
