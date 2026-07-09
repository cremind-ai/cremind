"""Client wrappers for the Backup & Restore REST endpoints.

Thin async functions over :class:`app.cli.client._base.Client`. No ``app.*``
server imports — the offline path in the command module talks to the engine
directly instead of going through here.
"""

from __future__ import annotations

import os
from typing import Any

from app.cli.client._base import Client


async def create(client: Client, passphrase: str | None = None) -> Any:
    body: dict[str, Any] = {}
    if passphrase:
        body["passphrase"] = passphrase
    return await client.post_json("/api/backup/create", body or None)


async def status(client: Client) -> Any:
    return await client.get_json("/api/backup/status")


async def list_backups(client: Client) -> Any:
    return await client.get_json("/api/backup/list")


async def download(client: Client, name: str, sink: Any) -> None:
    await client.download(f"/api/backup/download/{name}", sink)


async def upload(client: Client, path: str) -> Any:
    with open(path, "rb") as f:
        data = f.read()
    return await client.upload(
        "/api/backup/upload", files=[("file", (os.path.basename(path), data))]
    )


async def delete(client: Client, name: str) -> Any:
    return await client.delete(f"/api/backup/{name}")


async def restore(client: Client, name: str, passphrase: str | None = None) -> Any:
    body: dict[str, Any] = {"name": name}
    if passphrase:
        body["passphrase"] = passphrase
    return await client.post_json("/api/backup/restore", body)


async def restore_status(client: Client) -> Any:
    return await client.get_json("/api/backup/restore/status")


async def restore_report(client: Client) -> Any:
    return await client.get_json("/api/backup/restore/report")


async def ack_report(client: Client) -> Any:
    return await client.post_json("/api/backup/restore/report/ack", {})
