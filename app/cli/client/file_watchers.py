"""File-watcher endpoints — `/api/file-watchers*`.

Mirrors `cli/internal/client/filewatchers.go`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def list_file_watchers(client: Client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/file-watchers")
    if isinstance(resp, dict) and isinstance(resp.get("subscriptions"), list):
        return [s for s in resp["subscriptions"] if isinstance(s, dict)]
    return []


async def delete_file_watcher(client: Client, watcher_id: str) -> None:
    await client.delete(f"/api/file-watchers/{quote(watcher_id, safe='')}")


async def create_file_watcher(
    client: Client,
    body: dict[str, Any],
) -> dict[str, Any]:
    resp = await client.post_json("/api/file-watchers", body)
    return resp if isinstance(resp, dict) else {}


async def update_file_watcher(
    client: Client,
    watcher_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    resp = await client.patch_json(
        f"/api/file-watchers/{quote(watcher_id, safe='')}", fields,
    )
    return resp if isinstance(resp, dict) else {}


def file_watchers_admin_stream_path() -> str:
    return "/api/file-watchers/admin/stream"
