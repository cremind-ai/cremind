"""Server-ops endpoints — `/health`, `/version`,
`/api/services/tray-capabilities`, `/api/system/environment`, and
`POST /api/system/restart`.

The first three reads are unauthenticated (the upgrader and UI probe them
before, or in spite of, a token); the environment description and the restart
are admin-only, because the description names the server's paths and bind host.
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


async def get_server_environment(client: Client) -> dict[str, Any]:
    """The full install description (admin): channel, install mode, deployment,
    versions, paths, plus two nested blocks — `kubernetes` (namespace, release,
    Deployment/Service and the `kubectl port-forward` line that reconnects,
    `None` off Kubernetes) and `vnc` (how a browser reaches the desktop, and
    the tunnels that must exist first).

    Both are admin-only for the same reason the rest of this payload is:
    cluster object names and ready-to-run commands describe the deployment, not
    the product. The unauthenticated tray endpoint carries only
    `{enabled, access, novnc_path, novnc_port}` and no identity at all.
    """
    resp = await client.get_json("/api/system/environment")
    return resp if isinstance(resp, dict) else {}


async def restart_server(client: Client) -> dict[str, Any]:
    """POST the restart request. Returns the 202 body `{ok, pid, status}`."""
    resp = await client.post_json("/api/system/restart")
    return resp if isinstance(resp, dict) else {}
