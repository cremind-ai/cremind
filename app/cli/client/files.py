"""File endpoints — `/api/files*`.

Thin async wrappers over the file-serving API: directory listing, working
directory, download, multipart upload, and mkdir/move/delete. The
filesystem-watch SSE stream is opened via ``Client.stream(file_watch_path(...))``.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode


async def get_cwd(client) -> str:
    resp = await client.get_json("/api/files/cwd")
    if isinstance(resp, dict):
        return str(resp.get("cwd") or "")
    return ""


async def set_cwd(client, conversation_id: str, path: str) -> str:
    resp = await client.post_json(
        "/api/files/cwd",
        {"conversation_id": conversation_id, "path": path},
    )
    if isinstance(resp, dict):
        return str(resp.get("working_directory") or "")
    return ""


async def list_directory(
    client,
    path: str,
    *,
    show_hidden: bool = False,
    conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"path": path}
    if show_hidden:
        params["show_hidden"] = "1"
    if conversation_id:
        params["conversation_id"] = conversation_id
    resp = await client.get_json("/api/files/list", params=params)
    return resp if isinstance(resp, dict) else {}


async def download(
    client,
    path: str,
    sink: Any,
    *,
    conversation_id: Optional[str] = None,
) -> None:
    params: dict[str, Any] = {"path": path}
    if conversation_id:
        params["conversation_id"] = conversation_id
    await client.download("/api/files/open", sink, params=params)


async def upload(
    client,
    directory: str,
    files: list[tuple[str, bytes]],
    *,
    conversation_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Upload one or more (basename, bytes) parts into ``directory``."""
    data: dict[str, Any] = {"path": directory}
    if conversation_id:
        data["conversation_id"] = conversation_id
    parts = [("file", (name, blob)) for name, blob in files]
    resp = await client.upload("/api/files/upload", files=parts, data=data)
    if isinstance(resp, dict) and isinstance(resp.get("results"), list):
        return [r for r in resp["results"] if isinstance(r, dict)]
    return []


async def mkdir(client, path: str, *, conversation_id: Optional[str] = None) -> dict[str, Any]:
    body: dict[str, Any] = {"path": path}
    if conversation_id:
        body["conversation_id"] = conversation_id
    resp = await client.post_json("/api/files/mkdir", body)
    return resp if isinstance(resp, dict) else {}


async def move(
    client,
    src: str,
    dest: str,
    *,
    conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"src": src, "dest": dest}
    if conversation_id:
        body["conversation_id"] = conversation_id
    resp = await client.post_json("/api/files/move", body)
    return resp if isinstance(resp, dict) else {}


async def delete(client, path: str, *, conversation_id: Optional[str] = None) -> None:
    body: dict[str, Any] = {"path": path}
    if conversation_id:
        body["conversation_id"] = conversation_id
    await client.delete("/api/files/delete", body)


def file_watch_path(path: str, *, conversation_id: Optional[str] = None) -> str:
    params: dict[str, Any] = {"path": path}
    if conversation_id:
        params["conversation_id"] = conversation_id
    return f"/api/files/watch?{urlencode(params)}"
