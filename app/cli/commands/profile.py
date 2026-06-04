"""`cremind profile ...` — manage Cremind profiles, persona, and skill mode.

Mirrors `cli/cmd/profile.go`.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors


profile_app = typer.Typer(
    name="profile",
    help="Manage Cremind profiles, persona, and skill mode.",
    no_args_is_help=True,
)
persona_app = typer.Typer(
    name="persona",
    help="Manage a profile's persona text.",
    no_args_is_help=True,
)
skill_mode_app = typer.Typer(
    name="skill-mode",
    help="Get or set a profile's skill mode (manual/automatic).",
    no_args_is_help=True,
)
profile_app.add_typer(persona_app, name="persona")
profile_app.add_typer(skill_mode_app, name="skill-mode")


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
    """Show details for a profile (persona + skill mode)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_persona, get_skill_mode
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> tuple[str, str]:
        async with Client(cfg) as client:
            persona = await get_persona(client, name)
            skill_mode = await get_skill_mode(client, name)
            return persona, skill_mode

    persona, skill_mode = asyncio.run(_run())

    if mode.json:
        print_json({
            "name": name,
            "persona": persona,
            "skill_mode": skill_mode,
        })
        return

    print_kv([("name", name), ("skill_mode", skill_mode)])
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
) -> None:
    """Replace a profile's persona from stdin."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import set_persona
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    body = sys.stdin.read()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_persona(client, name, body)

    asyncio.run(_run())


@skill_mode_app.command("get")
@graceful_errors
def skill_mode_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Show the current skill mode."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_skill_mode
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await get_skill_mode(client, name)

    skill_mode = asyncio.run(_run())

    if mode.json:
        print_json({"mode": skill_mode})
    else:
        sys.stdout.write(f"{skill_mode}\n")


@skill_mode_app.command("set")
@graceful_errors
def skill_mode_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    skill_mode_value: str = typer.Argument(..., help="'manual' or 'automatic'.", metavar="MODE"),
) -> None:
    """Set skill mode to 'manual' or 'automatic'."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import set_skill_mode
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_skill_mode(client, name, skill_mode_value)

    asyncio.run(_run())
