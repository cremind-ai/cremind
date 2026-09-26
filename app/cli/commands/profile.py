"""`cremind profile ...` — manage Cremind profiles, persona, instructions, agent name.

Mirrors `cli/cmd/profile.go`.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors
from app.cli.commands.profile_wizard import wizard_app


profile_app = typer.Typer(
    name="profile",
    help="Manage Cremind profiles, persona, standing instructions, and agent name.",
    no_args_is_help=True,
)
persona_app = typer.Typer(
    name="persona",
    help="Manage a profile's persona text.",
    no_args_is_help=True,
)
instructions_app = typer.Typer(
    name="instructions",
    help="Manage a profile's standing instructions (task directives, not persona).",
    no_args_is_help=True,
)
agent_name_app = typer.Typer(
    name="agent-name",
    help="Get or set a profile's agent name (shown in chat and the @-mention menu).",
    no_args_is_help=True,
)
profile_app.add_typer(persona_app, name="persona")
profile_app.add_typer(instructions_app, name="instructions")
profile_app.add_typer(agent_name_app, name="agent-name")
# Its own module: the wizard carries a draft store, a step grammar and catalog
# validation, where everything else here is one call per command.
profile_app.add_typer(wizard_app, name="wizard")


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


@profile_app.command("use")
def profile_use(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile to activate for this terminal."),
) -> None:
    """Set the active profile for this terminal (remembered across commands)."""
    from app.cli import session

    if not session.has_token(name):
        available = session.list_profiles()
        hint = f" Available: {', '.join(available)}." if available else ""
        typer.echo(
            f"profile '{name}' has no token file under {session.tokens_dir()}.{hint}"
            f" Mint one with `cremind auth regenerate --local --profile {name}`.",
            err=True,
        )
        raise typer.Exit(code=1)
    session.set_session_profile(name)
    typer.echo(f"active profile for this terminal: {name}")


@profile_app.command("which")
def profile_which(ctx: typer.Context) -> None:
    """Show the profile remembered for this terminal (if any)."""
    from app.cli import session

    current = session.get_session_profile()
    if not current:
        typer.echo("no profile selected for this terminal", err=True)
        raise typer.Exit(code=1)
    typer.echo(current)


@profile_app.command("clear")
def profile_clear(ctx: typer.Context) -> None:
    """Forget this terminal's remembered profile (next command re-prompts)."""
    from app.cli import session

    session.clear_session_profile()
    typer.echo("cleared active profile for this terminal")


@profile_app.command("get")
@graceful_errors
def profile_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Show details for a profile (persona + instructions + agent name)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_agent_name, get_instructions, get_persona
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> tuple[str, str, str]:
        async with Client(cfg) as client:
            persona = await get_persona(client, name)
            instructions = await get_instructions(client, name)
            agent_name = await get_agent_name(client, name)
            return persona, instructions, agent_name

    persona, instructions, agent_name = asyncio.run(_run())

    if mode.json:
        print_json({
            "name": name,
            "persona": persona,
            "instructions": instructions,
            "agent_name": agent_name,
        })
        return

    print_kv([("name", name), ("agent_name", agent_name)])
    sys.stdout.write("\n--- persona ---\n")
    sys.stdout.write(persona)
    if not persona.endswith("\n"):
        sys.stdout.write("\n")
    # Omitted entirely when unset — most profiles have no standing instructions.
    if instructions.strip():
        sys.stdout.write("\n--- instructions ---\n")
        sys.stdout.write(instructions)
        if not instructions.endswith("\n"):
            sys.stdout.write("\n")


@profile_app.command("create")
@graceful_errors
def profile_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    working_dir: Optional[str] = typer.Option(
        None,
        "--working-dir",
        help=(
            "The new profile's working directory (absolute path; created if missing). "
            "Default: <workspaces>/<name>."
        ),
    ),
) -> None:
    """Create a new profile (admin only)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import create_profile
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await create_profile(client, name, working_dir)

    resp = asyncio.run(_run()) or {}
    sys.stdout.write(f"{name}\n")
    # stdout stays pipe-clean (the name alone); the advice goes to stderr.
    # What this command makes is a shell: no LLM, no tools, no channels and no
    # token, so nothing can act as it and it answers nothing. Most callers
    # asking for "a new profile" want the wizard instead.
    folder = resp.get("working_dir") if isinstance(resp, dict) else None
    if isinstance(folder, dict) and folder.get("path"):
        sys.stderr.write(f"Working directory: {folder['path']}\n")
    elif isinstance(folder, dict) and folder.get("error"):
        sys.stderr.write(f"Warning: its working directory could not be set up: {folder['error']}\n")
    sys.stderr.write(
        f"Created a bare profile: '{name}' has no LLM, tools, memory or channels, "
        "and no token yet.\n"
        f"Set it up and mint its token: cremind profile wizard start {name} --adopt\n"
    )


@profile_app.command("delete")
@graceful_errors
def profile_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    delete_working_dir: bool = typer.Option(
        False,
        "--delete-working-dir",
        help=(
            "Also delete its working directory and every file in it. Default: keep it "
            "(moved to <workspaces>/.deleted/). A folder chosen elsewhere is never touched."
        ),
    ),
) -> None:
    """Delete a profile (cascades conversations, tools, skills)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import delete_profile
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await delete_profile(client, name, delete_working_dir=delete_working_dir)

    resp = asyncio.run(_run()) or {}
    if mode.json:
        print_json(resp)
        return
    line = _describe_retired_working_dir(resp.get("working_dir"))
    if line:
        stream = sys.stderr if line.startswith("Warning:") else sys.stdout
        stream.write(f"{line}\n")


def _describe_retired_working_dir(info: object) -> str:
    """One line on what ``profile delete`` did with the working directory."""
    if not isinstance(info, dict):
        return ""
    action, path = info.get("action"), info.get("path") or ""
    if action == "archived":
        return f"Working directory kept: moved to {info.get('archived_to')}"
    if action == "deleted":
        return f"Working directory deleted: {path}"
    if action == "none":
        return f"Working directory {path} was empty or missing; nothing to keep."
    if action == "untouched":
        return f"Working directory left in place: {path} (not a folder Cremind made for it)"
    if action == "failed":
        return f"Warning: the working directory {path} could not be moved or deleted: {info.get('error')}"
    return ""


# ── working-dir ────────────────────────────────────────────────────────────


def _looks_like_profile_name(text: str) -> bool:
    # Same rule the server applies. A path always has a separator, a drive
    # colon or a ``~``, none of which a profile name may contain, so a lone
    # argument is never ambiguous.
    import re

    return bool(re.fullmatch(r"[a-z0-9_-]{1,64}", text))


@profile_app.command("working-dir")
@graceful_errors
def profile_working_dir(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(
        None, help="Profile (default: your own). May be omitted before PATH.",
    ),
    path: Optional[str] = typer.Argument(
        None, help="New folder, absolute (admin only). Omit to show the current one.",
    ),
    default: bool = typer.Option(
        False, "--default", help="Reset to the default folder, <workspaces>/<name> (admin only).",
    ),
) -> None:
    """Show a profile's working directory, or (admin) change it."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.me import get_me
    from app.cli.client.profiles import get_working_dir, set_working_dir
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if name is not None and path is None and not _looks_like_profile_name(name):
        name, path = None, name
    if path is not None and default:
        typer.echo("give a PATH or --default, not both", err=True)
        raise typer.Exit(code=1)

    async def _run() -> dict:
        async with Client(cfg) as client:
            target = name or (await get_me(client)).profile
            if not target:
                raise RuntimeError("could not tell which profile this token belongs to; name it")
            if default:
                return await set_working_dir(client, target, None)
            if path is not None:
                return await set_working_dir(client, target, path)
            return await get_working_dir(client, target)

    try:
        info = asyncio.run(_run())
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    if mode.json:
        print_json(info)
        return
    print_kv([
        ("profile", str(info.get("profile") or "")),
        ("path", str(info.get("path") or "")),
        ("default", "yes" if info.get("is_default") else "no"),
        ("default_path", str(info.get("default_path") or "")),
        ("exists", "yes" if info.get("exists") else "no"),
    ])


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


@instructions_app.command("get")
@graceful_errors
def instructions_get(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
) -> None:
    """Print a profile's standing instructions to stdout."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import get_instructions
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await get_instructions(client, name)

    instructions = asyncio.run(_run())

    if mode.json:
        print_json({"content": instructions})
    else:
        sys.stdout.write(instructions)


@instructions_app.command("set")
@graceful_errors
def instructions_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name."),
    content: Optional[str] = typer.Argument(
        None,
        help="Instructions text. If omitted, read from stdin (file redirect, heredoc, or pipe).",
    ),
) -> None:
    """Replace a profile's standing instructions from an argument or stdin."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.profiles import set_instructions
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    if content is not None:
        body = content
    else:
        usage = (
            "instructions set: provide the instructions text. Usage: "
            "`cremind profile instructions set <name> <text>` "
            "(or `cremind profile instructions set <name> < file.md`)."
        )
        if sys.stdin.isatty():
            typer.echo(usage, err=True)
            raise typer.Exit(code=1)
        body = sys.stdin.read()
        if not body.strip():
            # Empty pipe / `< /dev/null` / exec_shell's stdin auto-EOF: same
            # guard as `persona set` — don't silently wipe the instructions.
            # A deliberate clear stays possible: `instructions set <name> ""`.
            typer.echo(usage, err=True)
            raise typer.Exit(code=1)

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_instructions(client, name, body)

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
