"""Profile endpoints — `/api/profiles*`.

Mirrors `cli/internal/client/profiles.go`.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from app.cli.client._base import Client


async def list_profiles(client: Client) -> list[str]:
    resp = await client.get_json("/api/profiles")
    if isinstance(resp, dict):
        profiles = resp.get("profiles")
        if isinstance(profiles, list):
            return [str(p) for p in profiles]
    return []


async def create_profile(
    client: Client, name: str, working_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Create a bare profile. ``working_dir`` is the folder the admin picks for
    it; omitted, it gets its default. Returns the server's response."""
    body: dict[str, Any] = {"name": name}
    if working_dir:
        body["working_dir"] = working_dir
    resp = await client.post_json("/api/profiles", body)
    return resp if isinstance(resp, dict) else {}


async def delete_profile(
    client: Client, name: str, *, delete_working_dir: bool = False,
) -> dict[str, Any]:
    """Delete a profile. Its Cremind-made working directory is archived
    unless ``delete_working_dir``. Returns the server's response (its
    ``working_dir`` says what happened to the folder)."""
    resp = await client.delete(
        f"/api/profiles/{quote(name, safe='')}",
        params={"working_dir": "delete" if delete_working_dir else "keep"},
    )
    return resp if isinstance(resp, dict) else {}


async def get_working_dir(client: Client, name: str) -> dict[str, Any]:
    """``{profile, path, default_path, is_default, exists}``."""
    resp = await client.get_json(f"/api/profiles/{quote(name, safe='')}/working-dir")
    return resp if isinstance(resp, dict) else {}


async def set_working_dir(client: Client, name: str, path: Optional[str]) -> dict[str, Any]:
    """Admin only. ``path=None`` resets to the default folder."""
    resp = await client.put_json(
        f"/api/profiles/{quote(name, safe='')}/working-dir",
        {"path": path},
    )
    return resp if isinstance(resp, dict) else {}


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


async def get_instructions(client: Client, name: str) -> str:
    resp = await client.get_json(f"/api/profiles/{quote(name, safe='')}/instructions")
    if isinstance(resp, dict):
        return str(resp.get("content") or "")
    return ""


async def set_instructions(client: Client, name: str, content: str) -> None:
    await client.put_json(
        f"/api/profiles/{quote(name, safe='')}/instructions",
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
