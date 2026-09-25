"""User Document Search endpoints — `/api/userdocs/*`.

Everything is scoped to the caller's own profile except `admin`, which is the
server-wide gate and needs the admin profile. Two error bodies callers should
recognise:

- **409 ConfirmationRequired** — the change would remove indexed content; the
  body carries `plan` (what would go) and `confirm` (a token). Repeat the same
  request with `confirm` set to apply it.
- **409 FeatureNotInstalled** — allowing the feature needs optional extras;
  install them with `cremind features install userdocs`.
"""

from __future__ import annotations

from typing import Any, Optional

from app.cli.client._base import Client


async def get_status(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/userdocs/status")
    return resp if isinstance(resp, dict) else {}


async def get_settings(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/userdocs/settings")
    return resp if isinstance(resp, dict) else {}


async def put_settings(client: Client, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.put_json("/api/userdocs/settings", body)
    return resp if isinstance(resp, dict) else {}


async def validate_root(client: Client, path: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {"path": path} if path else {"root_mode": "inherit"}
    resp = await client.post_json("/api/userdocs/validate-root", body)
    return resp if isinstance(resp, dict) else {}


async def get_admin(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/userdocs/admin")
    return resp if isinstance(resp, dict) else {}


async def put_admin(client: Client, policy: dict[str, Any]) -> dict[str, Any]:
    resp = await client.put_json("/api/userdocs/admin", {"policy": policy})
    return resp if isinstance(resp, dict) else {}


def userdocs_stream_path() -> str:
    return "/api/userdocs/stream"
