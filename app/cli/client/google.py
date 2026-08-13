"""Google Suite account links — ``/api/google/*``.

Thin async wrappers over the inventory and the two unlink endpoints. Every one of
them returns the whole payload rather than a narrowed field: the server phrases
the consequence, the shared-grant warning and the failure prose (see
``app/api/google.py``), and the command has to be able to print them.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def get_accounts(client: Client) -> dict[str, Any]:
    """GET /api/google/accounts — auth required. Per-skill link state."""
    resp = await client.get_json("/api/google/accounts")
    return resp if isinstance(resp, dict) else {}


async def unlink_skill(
    client: Client, skill: str, *, revoke: bool = True, force_revoke: bool = False
) -> dict[str, Any]:
    """POST /api/google/accounts/{skill}/unlink — auth required."""
    resp = await client.post_json(
        f"/api/google/accounts/{quote(skill, safe='')}/unlink",
        {"revoke": revoke, "force_revoke": force_revoke},
    )
    return resp if isinstance(resp, dict) else {}


async def unlink_all(client: Client, *, revoke: bool = True) -> dict[str, Any]:
    """POST /api/google/unlink-all — auth required."""
    resp = await client.post_json("/api/google/unlink-all", {"revoke": revoke})
    return resp if isinstance(resp, dict) else {}
