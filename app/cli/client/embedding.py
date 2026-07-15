"""Vector-embedding subsystem endpoints — `/api/config/embedding*`.

`status`/`initialize`/`stream` are unauthenticated (the Setup Wizard polls them
before a token exists); `get`/`put` are admin-only. `put` can return **409
FeatureNotInstalled** with a `missing` list when the chosen provider's optional
extras aren't installed — install them with `cremind features install` first.
"""

from __future__ import annotations

from typing import Any

from app.cli.client._base import Client


async def get_status(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/config/embedding/status")
    return resp if isinstance(resp, dict) else {}


async def get_config(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/config/embedding")
    return resp if isinstance(resp, dict) else {}


async def put_config(client: Client, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.put_json("/api/config/embedding", body)
    return resp if isinstance(resp, dict) else {}


async def initialize(client: Client) -> dict[str, Any]:
    resp = await client.post_json("/api/config/embedding/initialize")
    return resp if isinstance(resp, dict) else {}


def embedding_stream_path() -> str:
    return "/api/config/embedding/stream"
