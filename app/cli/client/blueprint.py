"""Client wrappers for the Blueprint REST endpoints.

Thin async functions over :class:`app.cli.client._base.Client`. No ``app.*``
server imports — the offline ``inspect`` path in the command module reads the
archive's manifest via the engine directly instead of going through here.
"""

from __future__ import annotations

import os
from typing import Any

from app.cli.client._base import Client


async def get_exportable(client: Client) -> Any:
    return await client.get_json("/api/blueprints/exportable")


async def export_blueprint(client: Client, body: dict[str, Any]) -> Any:
    return await client.post_json("/api/blueprints/export", body)


async def list_blueprints(client: Client) -> Any:
    return await client.get_json("/api/blueprints")


async def download(client: Client, name: str, sink: Any) -> None:
    await client.download(f"/api/blueprints/download/{name}", sink)


async def delete(client: Client, name: str) -> Any:
    return await client.delete(f"/api/blueprints/{name}")


async def upload(client: Client, path: str, *, replace: bool = False) -> Any:
    with open(path, "rb") as f:
        data = f.read()
    params = {"replace": "true"} if replace else None
    # Low-level post so the ``?replace`` query param rides along with multipart.
    resp = await client._http.post(  # noqa: SLF001 — intentional low-level use
        "/api/blueprints/import/upload",
        params=params,
        files=[("file", (os.path.basename(path), data))],
    )
    client._check_response(resp)  # noqa: SLF001
    return resp.json() if resp.content else None


async def get_session(client: Client) -> Any:
    return await client.get_json("/api/blueprints/import/session")


async def apply_step(client: Client, key: str, body: dict[str, Any] | None) -> Any:
    return await client.post_json(f"/api/blueprints/import/steps/{key}", body or {})


async def skip_step(client: Client, key: str) -> Any:
    return await client.post_json(f"/api/blueprints/import/steps/{key}/skip", {})


async def finalize(client: Client) -> Any:
    return await client.post_json("/api/blueprints/import/finalize", {})


async def abort(client: Client, *, delete_profile: bool = True) -> Any:
    return await client.post_json(
        "/api/blueprints/import/abort", {"delete_profile": delete_profile}
    )
