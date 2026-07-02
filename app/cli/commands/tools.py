"""`cremind tools ...` — list and configure tools & skills.

Mirrors `cli/cmd/tools.go`.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


tools_app = typer.Typer(
    name="tools",
    help="List and configure tools & skills.",
    no_args_is_help=True,
)


@tools_app.command("list")
@graceful_errors
def tools_list(
    ctx: typer.Context,
    type_filter: Optional[str] = typer.Option(
        None,
        "--type",
        help="Filter by tool_type: built-in, mcp, a2a, skill, intrinsic.",
    ),
) -> None:
    """List all tools (built-in, mcp, a2a, skill, intrinsic)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import list_tools
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_tools(client, type_filter or "")

    tools = asyncio.run(_run())

    if mode.json:
        print_json(tools)
        return

    table = Table(mode, "TOOL_ID", "TYPE", "ENABLED", "CONFIGURED", "NAME")
    for row in tools:
        table.add_row(
            string_field(row, "tool_id"),
            string_field(row, "tool_type"),
            bool_field(row, "enabled", True),
            bool_field(row, "configured", False),
            string_field(row, "name"),
        )
    table.render()


@tools_app.command("get")
@graceful_errors
def tools_get(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Show detailed configuration for a tool."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import get_tool
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tool(client, tool_id)

    tool = asyncio.run(_run())

    if mode.json:
        print_json(tool)
        return

    print_kv([
        ("tool_id", string_field(tool, "tool_id")),
        ("name", string_field(tool, "name")),
        ("tool_type", string_field(tool, "tool_type")),
        ("description", string_field(tool, "description")),
        ("configured", bool_field(tool, "configured", False)),
    ])
    if isinstance(tool.get("config"), dict):
        sys.stdout.write("\n--- config ---\n")
        sys.stdout.write(_json.dumps(tool["config"], indent=2, ensure_ascii=False, default=str) + "\n")


@tools_app.command("enable")
@graceful_errors
def tools_enable(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Enable an A2A or MCP tool."""
    _set_tool_enabled(ctx, tool_id, True)


@tools_app.command("disable")
@graceful_errors
def tools_disable(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Disable an A2A or MCP tool."""
    _set_tool_enabled(ctx, tool_id, False)


def _set_tool_enabled(ctx: typer.Context, tool_id: str, enabled: bool) -> None:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_enabled
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_enabled(client, tool_id, enabled)

    asyncio.run(_run())


@tools_app.command("set-var")
@graceful_errors
def tools_set_var(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more KEY=VALUE pairs.",
        metavar="KEY=VALUE",
    ),
) -> None:
    """Set Tool Variables (env-style key/value pairs)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_variables
    from app.cli.config import Config

    if not pairs:
        typer.echo("expected at least one KEY=VALUE pair", err=True)
        raise typer.Exit(code=1)

    variables: dict[str, str] = {}
    for kv in pairs:
        if "=" not in kv:
            typer.echo(f"expected KEY=VALUE, got '{kv}'", err=True)
            raise typer.Exit(code=1)
        k, v = kv.split("=", 1)
        variables[k] = v

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_variables(client, tool_id, variables)

    asyncio.run(_run())


@tools_app.command("set-args")
@graceful_errors
def tools_set_args(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    json_body: str = typer.Option(
        ...,
        "--json",
        help="Tool arguments as a JSON object.",
    ),
) -> None:
    """Set Tool Arguments from a JSON object."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_arguments
    from app.cli.config import Config

    try:
        values = _json.loads(json_body)
    except _json.JSONDecodeError as e:
        typer.echo(f"invalid --json: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not isinstance(values, dict):
        typer.echo("--json must be an object", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_arguments(client, tool_id, values)

    asyncio.run(_run())


@tools_app.command("set-llm")
@graceful_errors
def tools_set_llm(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    provider: Optional[str] = typer.Option(None, "--provider", help="LLM provider."),
    model: Optional[str] = typer.Option(None, "--model", help="Model name."),
    reasoning_effort: Optional[str] = typer.Option(
        None, "--reasoning-effort", help="low | medium | high."
    ),
    full_reasoning: Optional[str] = typer.Option(
        None, "--full-reasoning", help="true | false."
    ),
) -> None:
    """Set LLM Parameters for a tool (partial - omitted flags unchanged)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_llm_params
    from app.cli.config import Config

    params: dict[str, Any] = {}
    if provider:
        params["llm_provider"] = provider
    if model:
        params["llm_model"] = model
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    if full_reasoning is not None and full_reasoning != "":
        v = full_reasoning.lower()
        if v == "true":
            params["full_reasoning"] = True
        elif v == "false":
            params["full_reasoning"] = False
        else:
            typer.echo("--full-reasoning must be 'true' or 'false'", err=True)
            raise typer.Exit(code=1)

    if not params:
        typer.echo(
            "at least one of --provider, --model, --reasoning-effort, --full-reasoning is required",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_llm_params(client, tool_id, params)

    asyncio.run(_run())


@tools_app.command("reset-llm")
@graceful_errors
def tools_reset_llm(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    keys: list[str] = typer.Argument(
        ...,
        help="Keys to revert to code defaults.",
        metavar="KEY",
    ),
) -> None:
    """Delete LLM-parameter overrides so code defaults apply."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import reset_tool_llm_params
    from app.cli.config import Config

    if not keys:
        typer.echo("expected at least one key to reset", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await reset_tool_llm_params(client, tool_id, keys)

    asyncio.run(_run())


@tools_app.command("leaves")
@graceful_errors
def tools_leaves(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """List a tool's sub-tools ("leaves") with their per-profile enabled state."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import list_tool_leaves
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_tool_leaves(client, tool_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    if out.get("disconnected"):
        sys.stderr.write("(tool is disconnected — live sub-tool list unavailable)\n")
    leaves = out.get("leaves") or []
    table = Table(mode, "LEAF", "NAME", "ENABLED", "DESCRIPTION")
    for leaf in leaves:
        if not isinstance(leaf, dict):
            continue
        table.add_row(
            string_field(leaf, "leaf_name"),
            string_field(leaf, "name"),
            bool_field(leaf, "enabled", True),
            string_field(leaf, "description"),
        )
    table.render()


@tools_app.command("set-leaf")
@graceful_errors
def tools_set_leaf(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more NAME=true|false sub-tool toggles.",
        metavar="NAME=BOOL",
    ),
) -> None:
    """Enable or disable a tool's sub-tools ("leaves")."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_leaves
    from app.cli.config import Config

    if not pairs:
        typer.echo("expected at least one NAME=true|false pair", err=True)
        raise typer.Exit(code=1)

    leaves: dict[str, bool] = {}
    for kv in pairs:
        if "=" not in kv:
            typer.echo(f"expected NAME=true|false, got '{kv}'", err=True)
            raise typer.Exit(code=1)
        name, raw = kv.split("=", 1)
        v = raw.strip().lower()
        if v in ("true", "1", "yes", "on"):
            leaves[name] = True
        elif v in ("false", "0", "no", "off"):
            leaves[name] = False
        else:
            typer.echo(f"'{raw}' is not a boolean (use true/false)", err=True)
            raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_leaves(client, tool_id, leaves)

    asyncio.run(_run())


@tools_app.command("register-long-running")
@graceful_errors
def tools_register_long_running(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the duplicate-command check.",
    ),
) -> None:
    """Spawn a skill's long_running_app and persist it as autostart."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import register_long_running_app
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await register_long_running_app(client, tool_id, force)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    print_kv([
        ("process_id", string_field(out, "process_id")),
        ("autostart_id", str(out.get("autostart_id", ""))),
        ("command", string_field(out, "command")),
        ("working_dir", string_field(out, "working_dir")),
    ])
