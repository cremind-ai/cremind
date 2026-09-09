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
    allow_unknown: bool = False,
) -> None:
    body: dict[str, Any] = {"variables": variables}
    if allow_unknown:
        body["allow_unknown"] = True
    await client.put_json(
        f"/api/tools/{quote(tool_id, safe='')}/variables",
        body,
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


async def get_tool_variable_options(
    client: Client,
    tool_id: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return ``{tool_id, variables: {VAR: {options, error, source}}}``.

    Live option lists for a tool's ``dynamic_options`` variables (e.g. the Claude
    models available to the logged-in account for ``claude_code``). Tools without
    a dynamic-options hook return an empty ``variables`` map.
    """
    params = {"refresh": "1"} if refresh else None
    out = await client.get_json(
        f"/api/tools/{quote(tool_id, safe='')}/variable-options", params=params,
    )
    return out if isinstance(out, dict) else {}


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


async def get_coding_agents(client: Client) -> list[dict[str, Any]]:
    """Return one row per coding delegate (Claude Code, Codex).

    Each row carries `tool_id`, `display_name`, `sdk_installed`, `enabled`,
    `credential_source`, `sign_in` and a human `message`. Served from
    `/api/coding-agents`, which is per-profile — the credential and enabled
    state are this profile's, not the server's.
    """
    resp = await client.get_json("/api/coding-agents")
    if isinstance(resp, dict) and isinstance(resp.get("agents"), list):
        return [a for a in resp["agents"] if isinstance(a, dict)]
    return []


async def probe_coding_agent(
    client: Client,
    tool_id: str,
    fresh: bool = False,
) -> dict[str, Any]:
    """Run the live sign-in check for one coding delegate.

    Returns the delegate's own status payload (`logged_in`, `probe_detail`,
    `message`, …) — the same one the agent sees, because the endpoint invokes
    the same status leaf.

    `fresh` bypasses the server's short probe cache. That cache exists so a
    burst of clicks costs one subprocess, but it also means a probe run
    *immediately after* a sign-in would be answered with the "not signed in"
    verdict recorded seconds earlier — which is exactly the moment the CLI asks.
    """
    body: dict[str, Any] = {"fresh": True} if fresh else {}
    out = await client.post_json(
        f"/api/coding-agents/{quote(tool_id, safe='')}/probe", body,
    )
    return out if isinstance(out, dict) else {}


async def get_coding_agent_cli(client: Client, tool_id: str) -> dict[str, Any]:
    """Describe the delegate's login CLI *as the server sees it*.

    Returns `{tool_id, binary, binary_source, login_argv, logout_argv,
    status_argv, profile_env, shared_env, server_hostname, system_dir,
    platform}`.

    The paths in it are the SERVER's. `cremind` may well be running on a
    different machine, and a login has to happen where the binary and the CLI
    home actually are — so the caller compares what came back against its own
    filesystem before trying to run any of it.
    """
    out = await client.get_json(f"/api/coding-agents/{quote(tool_id, safe='')}/cli")
    return out if isinstance(out, dict) else {}


async def logout_coding_agent(
    client: Client,
    tool_id: str,
    scope: str = "profile",
) -> dict[str, Any]:
    """Sign one coding delegate out; returns `{ok, scope, detail}`.

    Unlike signing in, this needs no binary on the caller's machine: the server
    runs the CLI's own logout against the resolved home and removes the
    credential file, so it works from a remote `cremind` just as well as from a
    shell on the host. `scope="shared"` targets the server-wide login every
    profile without one of its own inherits (admin only).
    """
    out = await client.post_json(
        f"/api/coding-agents/{quote(tool_id, safe='')}/logout", {"scope": scope},
    )
    return out if isinstance(out, dict) else {}


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
