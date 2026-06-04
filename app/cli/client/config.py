"""User-config endpoints — `/api/config/schema`, `/api/config/user`.

Mirrors `cli/internal/client/config.go`. The active profile is resolved
server-side from the JWT.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def get_config_schema(client: Client) -> dict[str, Any]:
    """GET /api/config/schema — schema (groups -> fields with types,
    defaults, descriptions, enums, min/max).
    """
    out = await client.get_json("/api/config/schema")
    return out if isinstance(out, dict) else {}


async def get_user_config(client: Client) -> dict[str, Any]:
    """GET /api/config/user — returns `{values, defaults}` for the
    authenticated profile.
    """
    out = await client.get_json("/api/config/user")
    return out if isinstance(out, dict) else {}


async def update_user_config(
    client: Client,
    values: dict[str, Any],
) -> None:
    """PUT /api/config/user — partial write of values. Server validates
    against the schema.
    """
    await client.put_json("/api/config/user", {"values": values})


async def reset_user_config_key(client: Client, key: str) -> None:
    """DELETE /api/config/user/{key} — revert to declared default."""
    await client.delete(f"/api/config/user/{quote(key, safe='')}")
