"""Google Drive per-file access endpoints — `/api/drive/*`.

Thin async wrappers over the Drive API: link status, the files Cremind can reach,
and the Google Picker grant round (start / poll / complete-from-paste).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote


async def get_status(client) -> dict[str, Any]:
    resp = await client.get_json("/api/drive/status")
    return resp if isinstance(resp, dict) else {}


async def list_files(
    client, *, page_token: Optional[str] = None, page_size: Optional[int] = None
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if page_token:
        params["page_token"] = page_token
    if page_size:
        params["page_size"] = page_size
    resp = await client.get_json("/api/drive/files", params=params or None)
    return resp if isinstance(resp, dict) else {}


async def start_grant(
    client,
    *,
    file_ids: Optional[list[str]] = None,
    allow_multiple: bool = True,
    allow_folders: bool = True,
    mime_types: Optional[list[str]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "allow_multiple": allow_multiple,
        "allow_folders": allow_folders,
    }
    if file_ids:
        body["file_ids"] = file_ids
    if mime_types:
        body["mime_types"] = mime_types
    resp = await client.post_json("/api/drive/grants", body)
    return resp if isinstance(resp, dict) else {}


async def grant_status(client, state: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/drive/grants/{quote(state, safe='')}")
    return resp if isinstance(resp, dict) else {}


async def complete_grant(client, redirect_url: str) -> dict[str, Any]:
    resp = await client.post_json("/api/drive/grants/complete", {"redirect_url": redirect_url})
    return resp if isinstance(resp, dict) else {}


async def cancel_grant(client, state: str) -> None:
    await client.delete(f"/api/drive/grants/{quote(state, safe='')}")
