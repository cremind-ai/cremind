"""`cremind setup ...` — first-run setup and server-wide configuration.

Mirrors `cli/cmd/setup.go`. Subcommands:

  setup status                  Check whether setup is complete (unauthed)
  setup complete                POST a setup payload; prints JWT (unauthed)
  setup reset-orphaned          Clear setup_complete when no profiles exist (unauthed)
  setup reconfigure             Reset setup_complete (admin auth)
  setup server-config get [k]   Read server config (admin auth)
  setup server-config set k=v…  Write server config (admin auth)
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors


setup_app = typer.Typer(
    name="setup",
    help="First-run setup wizard and server-wide configuration.",
    no_args_is_help=True,
)
server_config_app = typer.Typer(
    name="server-config",
    help="Read or write server-wide configuration (admin auth).",
    no_args_is_help=True,
)
setup_app.add_typer(server_config_app, name="server-config")


# ── status ─────────────────────────────────────────────────────────────────

@setup_app.command("status")
@graceful_errors
def setup_status(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Also check whether this profile exists.",
    ),
) -> None:
    """Show setup completion status (unauthenticated)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.setup import get_setup_status
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> dict:
        async with Client(cfg) as client:
            return await get_setup_status(client, profile)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    rows: list[tuple[str, str]] = [
        ("setup_complete", bool_field(out, "setup_complete", False)),
    ]
    if "profile_exists" in out:
        rows.append(("profile_exists", bool_field(out, "profile_exists", False)))
    if "has_profiles" in out:
        rows.append(("has_profiles", bool_field(out, "has_profiles", False)))
    print_kv(rows)


# ── complete ───────────────────────────────────────────────────────────────

@setup_app.command("complete")
@graceful_errors
def setup_complete(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="Profile name (overrides the JSON field).",
    ),
    json_body: Optional[str] = typer.Option(
        None,
        "--json",
        help="Setup payload as a JSON string.",
    ),
    json_file: Optional[str] = typer.Option(
        None,
        "--json-file",
        help="Path to a JSON file containing the setup payload (use - for stdin).",
    ),
) -> None:
    """POST a setup payload; prints the resulting JWT (unauthenticated).

    Payload shape mirrors the setup wizard: profile, server_config,
    llm_config, tool_configs, agent_configs.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.setup import complete_setup
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    body = _read_setup_payload(json_body, json_file)
    if profile is not None:
        body["profile"] = profile
    if "profile" not in body:
        typer.echo(
            "a 'profile' field is required (use --profile or include it in the JSON)",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run():
        async with Client(cfg) as client:
            return await complete_setup(client, body)

    resp = asyncio.run(_run())

    if mode.json:
        print_json({
            "success": resp.success,
            "token": resp.token,
            "expires_at": resp.expires_at,
            "profile": resp.profile,
        })
    else:
        print_kv([
            ("profile", resp.profile),
            ("expires_at", resp.expires_at),
            ("token", resp.token),
        ])
        sys.stderr.write("\n")
        sys.stderr.write("Export the token to use the CLI:\n")
        sys.stderr.write(f"  export CREMIND_TOKEN={resp.token}\n")


def _read_setup_payload(
    inline: Optional[str],
    file: Optional[str],
) -> dict:
    """Resolve the setup payload from --json, --json-file, or stdin."""
    import json
    from pathlib import Path

    if inline and file:
        typer.echo("--json and --json-file are mutually exclusive", err=True)
        raise typer.Exit(code=1)

    if inline:
        raw = inline.encode()
    elif file is None or file == "-":
        raw = sys.stdin.buffer.read()
    else:
        try:
            raw = Path(file).read_bytes()
        except OSError as e:
            typer.echo(f"read {file}: {e}", err=True)
            raise typer.Exit(code=1) from e

    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        typer.echo(f"parse setup JSON: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not isinstance(body, dict):
        typer.echo("setup payload must be a JSON object", err=True)
        raise typer.Exit(code=1)
    return body


# ── reset-orphaned ─────────────────────────────────────────────────────────

@setup_app.command("reset-orphaned")
@graceful_errors
def setup_reset_orphaned(ctx: typer.Context) -> None:
    """Clear setup_complete when no profiles exist (unauthenticated)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.setup import reset_orphaned_setup
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]

    async def _run() -> None:
        async with Client(cfg) as client:
            await reset_orphaned_setup(client)

    asyncio.run(_run())


# ── reconfigure ────────────────────────────────────────────────────────────

@setup_app.command("reconfigure")
@graceful_errors
def setup_reconfigure(ctx: typer.Context) -> None:
    """Reset setup_complete so the wizard can run again (admin auth)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.setup import reconfigure as reconfigure_call
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await reconfigure_call(client)

    asyncio.run(_run())


# ── server-config get ──────────────────────────────────────────────────────

@server_config_app.command("get")
@graceful_errors
def server_config_get(
    ctx: typer.Context,
    key: Optional[str] = typer.Argument(None, help="Single config key to fetch."),
) -> None:
    """Show all server config values, or a single key when given."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.setup import get_server_config
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict:
        async with Client(cfg) as client:
            return await get_server_config(client)

    server_cfg = asyncio.run(_run())

    if key is not None:
        v = server_cfg.get(key)
        if mode.json:
            print_json({key: v})
        else:
            sys.stdout.write(f"{v if v is not None else ''}\n")
        return

    if mode.json:
        print_json(server_cfg)
        return
    print_map(server_cfg)


# ── server-config set ──────────────────────────────────────────────────────

@server_config_app.command("set")
@graceful_errors
def server_config_set(
    ctx: typer.Context,
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more KEY=VALUE pairs.",
        metavar="KEY=VALUE",
    ),
) -> None:
    """Write one or more server config keys."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.setup import update_server_config
    from app.cli.config import Config

    if not pairs:
        typer.echo("expected at least one KEY=VALUE pair", err=True)
        raise typer.Exit(code=1)

    values: dict = {}
    for kv in pairs:
        if "=" not in kv:
            typer.echo(f"expected KEY=VALUE, got '{kv}'", err=True)
            raise typer.Exit(code=1)
        k, v = kv.split("=", 1)
        values[k] = v

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await update_server_config(client, values)

    asyncio.run(_run())
