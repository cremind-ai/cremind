"""Server-ops endpoints — `/health`, `/version`,
`/api/services/tray-capabilities`, and `POST /api/system/restart`.

The three reads are unauthenticated (the upgrader and UI probe them before, or
in spite of, a token); the restart is admin-only.
"""

from __future__ import annotations

from typing import Any

from app.cli.client._base import Client


async def get_health(client: Client) -> tuple[int, Any]:
    """Return `(status_code, body)`. `/health` answers **503** when degraded,
    so this must not raise — the status code is part of the signal.
    """
    return await client.get_json_status("/health")


async def get_server_version(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/version")
    return resp if isinstance(resp, dict) else {}


async def get_tray_capabilities(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/services/tray-capabilities")
    return resp if isinstance(resp, dict) else {}


async def restart_server(client: Client) -> dict[str, Any]:
    """POST the restart request. Returns the 202 body `{ok, pid, status}`."""
    resp = await client.post_json("/api/system/restart")
    return resp if isinstance(resp, dict) else {}
