"""`cremind agents ...` — manage MCP server registrations.

Mirrors `cli/cmd/agents.go`.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


agents_app = typer.Typer(
    name="agents",
    help="Manage MCP server registrations.",
    no_args_is_help=True,
)
agents_config_app = typer.Typer(
    name="config",
    help="Read or update an MCP / built-in agent's per-profile config.",
    no_args_is_help=True,
)
agents_app.add_typer(agents_config_app, name="config")


@agents_app.command("list")
@graceful_errors
def agents_list(ctx: typer.Context) -> None:
    """List registered MCP tools with auth + enabled status."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import list_agents
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_agents(client)

    agents = asyncio.run(_run())

    if mode.json:
        print_json(agents)
        return

    table = Table(mode, "TOOL_ID", "TYPE", "ENABLED", "STATUS", "URL")
    for a in agents:
        table.add_row(
            string_field(a, "tool_id"),
            string_field(a, "agent_type"),
            bool_field(a, "enabled", False),
            string_field(a, "status_text"),
            string_field(a, "url"),
        )
    table.render()


@agents_app.command("add")
@graceful_errors
def agents_add(
    ctx: typer.Context,
    url: Optional[str] = typer.Option(None, "--url", help="MCP server URL (HTTP/SSE)."),
    json_config: Optional[str] = typer.Option(
        None, "--json-config",
        help="VS Code-style MCP server JSON (for stdio servers).",
    ),
    description: Optional[str] = typer.Option(None, "--description", help="MCP server description."),
) -> None:
    """Register a new MCP server."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import add_agent
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import string_field

    if not url and not json_config:
        typer.echo("either --url or --json-config is required", err=True)
        raise typer.Exit(code=1)

    body: dict[str, Any] = {"type": "mcp"}
    if url:
        body["url"] = url
    if json_config:
        body["json_config"] = json_config
    if description:
        body["description"] = description

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await add_agent(client, body)

    agent = asyncio.run(_run())

    if mode.json:
        print_json(agent)
        return
    print_kv([
        ("tool_id", string_field(agent, "tool_id")),
        ("name", string_field(agent, "name")),
        ("agent_type", string_field(agent, "agent_type")),
        ("url", string_field(agent, "url")),
        ("status_text", string_field(agent, "status_text")),
    ])


@agents_app.command("delete")
@graceful_errors
def agents_delete(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Unregister an MCP tool."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import remove_agent
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await remove_agent(client, tool_id)

    asyncio.run(_run())


@agents_app.command("enable")
@graceful_errors
def agents_enable(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Enable an agent for the active profile."""
    _set_agent_enabled(ctx, tool_id, True)


@agents_app.command("disable")
@graceful_errors
def agents_disable(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Disable an agent for the active profile."""
    _set_agent_enabled(ctx, tool_id, False)


def _set_agent_enabled(ctx: typer.Context, tool_id: str, enabled: bool) -> None:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import set_agent_enabled
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_agent_enabled(client, tool_id, enabled)

    asyncio.run(_run())


@agents_app.command("reconnect")
@graceful_errors
def agents_reconnect(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Retry a stub agent's connection."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import reconnect_agent
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await reconnect_agent(client, tool_id)

    asyncio.run(_run())


@agents_app.command("auth-url")
@graceful_errors
def agents_auth_url(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    return_url: Optional[str] = typer.Option(
        None, "--return-url",
        help="Optional URL to redirect to after the OAuth callback completes.",
    ),
) -> None:
    """Print the OAuth authorize URL for an agent."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import get_agent_auth_url
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await get_agent_auth_url(client, tool_id, return_url or "")

    auth_url = asyncio.run(_run())

    if mode.json:
        print_json({"auth_url": auth_url})
    else:
        sys.stdout.write(f"{auth_url}\n")


@agents_app.command("unlink")
@graceful_errors
def agents_unlink(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Drop the active profile's OAuth token for an agent."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import unlink_agent
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await unlink_agent(client, tool_id)

    asyncio.run(_run())


# ── config ────────────────────────────────────────────────────────────────

@agents_config_app.command("get")
@graceful_errors
def agents_config_get(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Show the agent's config (URL + description)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import get_agent_config
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_agent_config(client, tool_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")


@agents_config_app.command("set")
@graceful_errors
def agents_config_set(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    description: Optional[str] = typer.Option(None, "--description", help="Description."),
) -> None:
    """Patch an agent's config (only specified flags change)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.agents import update_agent_config
    from app.cli.config import Config

    body: dict[str, Any] = {}
    if description:
        body["description"] = description

    if not body:
        typer.echo("at least one config flag is required", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await update_agent_config(client, tool_id, body)

    asyncio.run(_run())
