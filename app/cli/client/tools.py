"""Tool endpoints — `/api/tools*`.

Mirrors `cli/internal/client/tools.go`. Each tool entry is a loose dict
because the schema varies by tool_type.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def list_tools(
    client: Client,
    type_filter: str = "",
) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/tools")
    tools: list[dict[str, Any]] = []
    if isinstance(resp, dict) and isinstance(resp.get("tools"), list):
        tools = [t for t in resp["tools"] if isinstance(t, dict)]
    if not type_filter:
        return tools
    return [t for t in tools if str(t.get("tool_type") or "") == type_filter]


async def get_tool(client: Client, tool_id: str) -> dict[str, Any]:
    out = await client.get_json(f"/api/tools/{quote(tool_id, safe='')}")
    return out if isinstance(out, dict) else {}


async def get_tool_arguments(client: Client, tool_id: str) -> dict[str, Any]:
    """Derive a tool's arguments view from `GET /api/tools/{id}`.

    There is no dedicated GET arguments endpoint; the tool detail already
    carries the `arguments_schema` plus the saved values under
    `config.arguments`. Complements `set_tool_arguments`.
    """
    tool = await get_tool(client, tool_id)
    config = tool.get("config") if isinstance(tool.get("config"), dict) else {}
    return {
        "arguments_schema": tool.get("arguments_schema"),
        "arguments": config.get("arguments"),
    }


async def set_tool_variables(
    client: Client,
    tool_id: str,
    variables: dict[str, str],
) -> None:
    await client.put_json(
        f"/api/tools/{quote(tool_id, safe='')}/variables",
        {"variables": variables},
    )


async def set_tool_arguments(
    client: Client,
    tool_id: str,
    arguments: dict[str, Any],
) -> None:
    await client.put_json(
        f"/api/tools/{quote(tool_id, safe='')}/arguments",
        {"arguments": arguments},
    )


async def set_tool_enabled(
    client: Client,
    tool_id: str,
    enabled: bool,
) -> None:
    await client.put_json(
        f"/api/tools/{quote(tool_id, safe='')}/enabled",
        {"enabled": enabled},
    )


async def list_tool_leaves(client: Client, tool_id: str) -> dict[str, Any]:
    """Return ``{supports_leaf_toggle, disconnected, leaves: [...]}``."""
    out = await client.get_json(f"/api/tools/{quote(tool_id, safe='')}/leaves")
    return out if isinstance(out, dict) else {}


async def set_tool_leaves(
    client: Client,
    tool_id: str,
    leaves: dict[str, bool],
) -> None:
    await client.put_json(
        f"/api/tools/{quote(tool_id, safe='')}/leaves",
        {"leaves": leaves},
    )


async def register_long_running_app(
    client: Client,
    tool_id: str,
    force: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if force:
        body["force"] = True
    out = await client.post_json(
        f"/api/tools/{quote(tool_id, safe='')}/long-running-app/register",
        body,
    )
    return out if isinstance(out, dict) else {}
