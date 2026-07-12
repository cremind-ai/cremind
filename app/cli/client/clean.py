"""Client wrapper for the per-profile clean/reset endpoint (`POST /api/clean`).

Thin async function over :class:`app.cli.client._base.Client`. No ``app.*`` server
imports (CLI import discipline — see ``app/cli/main.py``).
"""

from __future__ import annotations

from typing import Any

from app.cli.client._base import Client


async def clean(
    client: Client, scope: str, components: list[str] | None = None
) -> dict[str, Any]:
    """Clean the caller's own profile. ``scope`` is 'custom' | 'working' | 'factory';
    ``components`` is only sent (and required) for the 'custom' scope."""
    body: dict[str, Any] = {"scope": scope}
    if components:
        body["components"] = components
    resp = await client.post_json("/api/clean", body)
    return resp if isinstance(resp, dict) else {}
