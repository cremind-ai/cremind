"""Skill lifecycle endpoints — `/api/skills/*`.

Skills are *listed* and *configured* through `/api/tools` (each skill surfaces
as a `ToolType.SKILL` tool). These endpoints cover only the install/uninstall
lifecycle: import from an archive / public GitHub repo / Cremind Hub link, and
delete (external → permanent; built-in → reset-to-default).

Import responses are `{"success": true, "installed": [names],
"skipped": [{"name", "reason"}]}`; delete is `{"success": true, "reset": bool}`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def import_archive(
    client: Client, filename: str, data: bytes,
) -> dict[str, Any]:
    """Upload a skill archive (multipart; server keys off the part filename)."""
    resp = await client.upload(
        "/api/skills/import/archive",
        files=[("file", (filename, data))],
    )
    return resp if isinstance(resp, dict) else {}


async def import_github(client: Client, url: str) -> dict[str, Any]:
    resp = await client.post_json("/api/skills/import/github", {"url": url})
    return resp if isinstance(resp, dict) else {}


async def import_hub(client: Client, link: str) -> dict[str, Any]:
    resp = await client.post_json("/api/skills/import/hub", {"link": link})
    return resp if isinstance(resp, dict) else {}


async def delete_skill(client: Client, tool_id: str) -> dict[str, Any]:
    resp = await client.delete(f"/api/skills/{quote(tool_id, safe='')}")
    return resp if isinstance(resp, dict) else {}
