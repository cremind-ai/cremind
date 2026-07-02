"""`cremind profile ...` — manage Cremind profiles, persona, and agent name.

Mirrors `cli/cmd/profile.go`.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors


profile_app = typer.Typer(
    name="profile",
    help="Manage Cremind profiles, persona, and agent name.",
    no_args_is_help=True,
)
persona_app = typer.Typer(
    name="persona",
    help="Manage a profile's persona text.",
    no_args_is_help=True,
)
agent_name_app = typer.Typer(
    name="agent-name",
    help="Get or set a profile's agent name (shown in chat and the @-mention menu).",
    no_args_is_help=True,
)
profile_app.add_typer(persona_app, name="persona")
profile_app.add_typer(agent_name_app, name="agent-name")


@profile_app.command("list")
@graceful_errors
def profile_list(ctx: typer.Context) -> None:
    """List profiles."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import list_profiles
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[str]:
        async with Client(cfg) as client:
            return await list_profiles(client)

    profiles = asyncio.run(_run())

    if mode.json:
        print_json(profiles)
        return
    table = Table(mode, "PROFILE")
    for p in profiles:
        table.add_row(p)
    table.render()


@profile_app.command("get")
@graceful_errors
def profile_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Show details for a profile (persona + agent name)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_agent_name, get_persona
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> tuple[str, str]:
        async with Client(cfg) as client:
            persona = await get_persona(client, name)
            agent_name = await get_agent_name(client, name)
            return persona, agent_name

    persona, agent_name = asyncio.run(_run())

    if mode.json:
        print_json({
            "name": name,
            "persona": persona,
            "agent_name": agent_name,
        })
        return

    print_kv([("name", name), ("agent_name", agent_name)])
    sys.stdout.write("\n--- persona ---\n")
    sys.stdout.write(persona)
    if not persona.endswith("\n"):
        sys.stdout.write("\n")


@profile_app.command("create")
@graceful_errors
def profile_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Create a new profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import create_profile
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await create_profile(client, name)

    asyncio.run(_run())
    sys.stdout.write(f"{name}\n")


@profile_app.command("delete")
@graceful_errors
def profile_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Delete a profile (cascades conversations, tools, skills)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import delete_profile
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_profile(client, name)

    asyncio.run(_run())


@persona_app.command("get")
@graceful_errors
def persona_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Print a profile's persona to stdout."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_persona
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await get_persona(client, name)

    persona = asyncio.run(_run())

    if mode.json:
        print_json({"content": persona})
    else:
        sys.stdout.write(persona)


@persona_app.command("set")
@graceful_errors
def persona_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    content: Optional[str] = typer.Argument(
        None,
        help="Persona text. If omitted, read from stdin (file redirect, heredoc, or pipe).",
    ),
) -> None:
    """Replace a profile's persona from an argument or stdin."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import set_persona
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    if content is not None:
        body = content
    else:
        usage = (
            "persona set: provide the persona text. Usage: "
            "`cremind profile persona set <name> <text>` "
            "(or `cremind profile persona set <name> < file.md`)."
        )
        if sys.stdin.isatty():
            typer.echo(usage, err=True)
            raise typer.Exit(code=1)
        body = sys.stdin.read()
        if not body.strip():
            # Empty pipe / `< /dev/null` / exec_shell's stdin auto-EOF: treat
            # "no persona text" the same as the interactive case rather than
            # silently PUTting an empty persona. (A deliberate clear is still
            # possible via an explicit empty argument: `persona set <name> ""`.)
            typer.echo(usage, err=True)
            raise typer.Exit(code=1)

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_persona(client, name, body)

    asyncio.run(_run())


@agent_name_app.command("get")
@graceful_errors
def agent_name_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Show the profile's agent name."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_agent_name
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await get_agent_name(client, name)

    agent_name = asyncio.run(_run())

    if mode.json:
        print_json({"name": agent_name})
    else:
        sys.stdout.write(f"{agent_name}\n")


@agent_name_app.command("set")
@graceful_errors
def agent_name_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    agent_name_value: str = typer.Argument(..., help="New agent name (max 128 chars).", metavar="AGENT_NAME"),
) -> None:
    """Set the profile's agent name."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import set_agent_name
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_agent_name(client, name, agent_name_value)

    asyncio.run(_run())
