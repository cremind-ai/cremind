"""User Document Search endpoints — `/api/userdocs/*`.

Everything is scoped to the caller's own profile except `admin`, which is the
server-wide gate and needs the admin profile. Two error bodies callers should
recognise:

- **409 ConfirmationRequired** — the change would remove indexed content; the
  body carries `plan` (what would go) and `confirm` (a token). Repeat the same
  request with `confirm` set to apply it.
- **409 FeatureNotInstalled** — allowing the feature needs optional extras;
  install them with `cremind features install userdocs`.

`query/{find|search|read}` run the agent's search leaves and answer with the
text the agent would read; `citations/resolve` looks citation tokens up.
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


async def control(client: Client, action: str, **params: Any) -> dict[str, Any]:
    resp = await client.post_json("/api/userdocs/control", {"action": action, **params})
    return resp if isinstance(resp, dict) else {}


async def list_files(client: Client, **params: Any) -> dict[str, Any]:
    clean = {k: v for k, v in params.items() if v is not None}
    resp = await client.get_json("/api/userdocs/files", params=clean or None)
    return resp if isinstance(resp, dict) else {}


async def file_detail(client: Client, fid: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/userdocs/files/{fid}")
    return resp if isinstance(resp, dict) else {}


async def activity(client: Client, *, before: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if before is not None:
        params["before"] = before
    resp = await client.get_json("/api/userdocs/activity", params=params)
    return resp if isinstance(resp, dict) else {}


async def get_estimate(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/userdocs/estimate")
    return resp if isinstance(resp, dict) else {}


async def start_estimate(client: Client) -> dict[str, Any]:
    resp = await client.post_json("/api/userdocs/estimate", {})
    return resp if isinstance(resp, dict) else {}


async def get_storage(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/userdocs/storage")
    return resp if isinstance(resp, dict) else {}


def userdocs_stream_path() -> str:
    return "/api/userdocs/stream"


async def query(client: Client, leaf: str, body: dict[str, Any]) -> dict[str, Any]:
    """``leaf`` is ``find``, ``search`` or ``read``; ``body`` takes the same
    arguments as the agent's ``user_documents__*`` functions. The answer
    carries ``text`` (what the agent would read) plus the structured result."""
    clean = {k: v for k, v in body.items() if v is not None}
    resp = await client.post_json(f"/api/userdocs/query/{leaf}", clean)
    return resp if isinstance(resp, dict) else {}


async def resolve_citations(
    client: Client, tokens: list[str], *, conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve ``[ud:…]`` tokens to their file, location and snippet. With a
    conversation id, each is also checked against what the tools issued there."""
    body: dict[str, Any] = {"tokens": tokens}
    if conversation_id:
        body["conversation_id"] = conversation_id
    resp = await client.post_json("/api/userdocs/citations/resolve", body)
    return resp if isinstance(resp, dict) else {}
