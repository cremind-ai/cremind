"""Setup endpoints — `/api/config/setup*`, `/api/config/server`, `/api/config/reconfigure`.

Mirrors `cli/internal/client/setup.go`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.cli.client._base import Client


@dataclass(frozen=True)
class SetupResponse:
    success: bool
    token: str
    expires_at: str
    profile: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SetupResponse":
        return cls(
            success=bool(d.get("success") or False),
            token=str(d.get("token") or ""),
            expires_at=str(d.get("expires_at") or ""),
            profile=str(d.get("profile") or ""),
        )


async def get_setup_status(
    client: Client,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """GET /api/config/setup-status — returns
    `{setup_complete, profile_exists?, has_profiles?}`. Unauthenticated.
    """
    params = {"profile": profile} if profile else None
    out = await client.get_json("/api/config/setup-status", params=params)
    return out if isinstance(out, dict) else {}


async def complete_setup(
    client: Client,
    body: dict[str, Any],
) -> SetupResponse:
    """POST /api/config/setup — unauthenticated. Returns the JWT token."""
    data = await client.post_json("/api/config/setup", body)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected /api/config/setup response: {type(data).__name__}")
    return SetupResponse.from_dict(data)


async def reset_orphaned_setup(client: Client) -> None:
    """POST /api/config/reset-orphaned-setup — unauthenticated."""
    await client.post_json("/api/config/reset-orphaned-setup")


async def reconfigure(client: Client) -> None:
    """POST /api/config/reconfigure — admin auth required."""
    await client.post_json("/api/config/reconfigure")


async def get_server_config(client: Client) -> dict[str, Any]:
    """GET /api/config/server — returns `{config: {...}}`; this returns the
    inner `config` dict.
    """
    resp = await client.get_json("/api/config/server")
    if isinstance(resp, dict) and isinstance(resp.get("config"), dict):
        return resp["config"]
    return {}


async def update_server_config(
    client: Client,
    values: dict[str, Any],
) -> None:
    """PUT /api/config/server — partial write. The special key `jwt_secret`
    is stored as a secret server-side.
    """
    await client.put_json("/api/config/server", {"config": values})
