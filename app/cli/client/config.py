"""Config endpoints — `/api/config/schema`, `/api/config/user`, `/api/config/export`.

Mirrors `cli/internal/client/config.go`. The active profile is resolved
server-side from the JWT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
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


@dataclass(frozen=True)
class ConfigExport:
    """One rendered configuration file, as the server named it."""

    filename: str
    content: bytes
    content_type: str
    #: ``full`` (admin) or ``profile`` (the reduced per-profile file).
    scope: str


_FILENAME_RE = re.compile(r'filename="([^"]+)"')


async def export_config(
    client: Client,
    fmt: str = "md",
    *,
    agent_url: Optional[str] = None,
    pending_https: bool = False,
) -> ConfigExport:
    """GET /api/config/export — the configuration file for the token's profile.

    Rendered server-side, so the CLI and the web UI cannot produce different
    files for the same install. The ``admin`` profile gets every section; any
    other profile gets a reduced file with its own identity and none of the
    install-wide database / vector-store / desktop / Kubernetes detail.

    ``agent_url`` overrides the address the file names — needed wherever this
    process knows the browser-reachable origin better than the server does (a
    split-origin dev box, or a wizard mid-HTTPS-pivot).
    """
    params: dict[str, Any] = {"format": fmt}
    if agent_url:
        params["agent_url"] = agent_url
    if pending_https:
        params["pending_https"] = "1"
    content, headers = await client.get_bytes("/api/config/export", params=params)
    match = _FILENAME_RE.search(headers.get("content-disposition", ""))
    return ConfigExport(
        filename=match.group(1) if match else f"cremind-config.{fmt}",
        content=content,
        content_type=(headers.get("content-type") or "text/plain").split(";")[0].strip(),
        scope=headers.get("x-cremind-export-scope") or "full",
    )
