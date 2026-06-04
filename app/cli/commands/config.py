"""`cremind config ...` — read and write user_config (per-profile general settings).

Mirrors `cli/cmd/config.go`. Subcommands:

  config schema             Print the user_config schema
  config get [<group.key>]  Show all values, or a single key
  config set <key> <value>  Set a config key for the active profile
  config reset <key>        Revert a config key to its default
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


config_app = typer.Typer(
    name="config",
    help="Read and write user_config (per-profile general settings).",
    no_args_is_help=True,
)


# ── schema ─────────────────────────────────────────────────────────────────

@config_app.command("schema")
@graceful_errors
def config_schema(ctx: typer.Context) -> None:
    """Print the user_config schema (groups, fields, types, defaults)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.config import get_config_schema
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict:
        async with Client(cfg) as client:
            return await get_config_schema(client)

    schema = asyncio.run(_run())

    if mode.json:
        print_json(schema)
        return

    groups = schema.get("groups") if isinstance(schema, dict) else None
    if not isinstance(groups, dict):
        return
    for group_name in sorted(groups.keys()):
        group = groups[group_name] if isinstance(groups[group_name], dict) else {}
        sys.stdout.write(f"[{group_name}] {string_field(group, 'label')}\n")
        desc = string_field(group, "description")
        if desc:
            sys.stdout.write(f"  {desc}\n")
        fields = group.get("fields") if isinstance(group, dict) else None
        if isinstance(fields, dict):
            for field_name in sorted(fields.keys()):
                f = fields[field_name] if isinstance(fields[field_name], dict) else {}
                f_type = string_field(f, "type")
                default = f.get("default")
                sys.stdout.write(
                    f"  {group_name}.{field_name}  type={f_type} default={default}\n"
                )
        sys.stdout.write("\n")


# ── get ────────────────────────────────────────────────────────────────────

@config_app.command("get")
@graceful_errors
def config_get(
    ctx: typer.Context,
    key: Optional[str] = typer.Argument(None, help="Single 'group.key' to fetch."),
) -> None:
    """Show all config values, or a single key when given."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.config import get_user_config
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict:
        async with Client(cfg) as client:
            return await get_user_config(client)

    user_cfg = asyncio.run(_run())
    values = user_cfg.get("values") if isinstance(user_cfg.get("values"), dict) else {}
    defaults = user_cfg.get("defaults") if isinstance(user_cfg.get("defaults"), dict) else {}

    if key is not None:
        v: Any = values.get(key) if values.get(key) is not None else defaults.get(key)
        if mode.json:
            print_json({key: v})
        else:
            sys.stdout.write(f"{v if v is not None else ''}\n")
        return

    if mode.json:
        print_json(user_cfg)
        return

    table = Table(mode, "KEY", "VALUE", "DEFAULT")
    for k in sorted(defaults.keys()):
        v = values.get(k)
        display = "" if v is None else str(v)
        table.add_row(k, display, str(defaults.get(k, "")))
    table.render()


# ── set ────────────────────────────────────────────────────────────────────

@config_app.command("set")
@graceful_errors
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="'group.key' to set.", metavar="GROUP.KEY"),
    value: str = typer.Argument(..., help="Value to set."),
) -> None:
    """Set a config key for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.config import update_user_config
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    coerced = _coerce_config_value(value)

    async def _run() -> None:
        async with Client(cfg) as client:
            await update_user_config(client, {key: coerced})

    asyncio.run(_run())


# ── reset ──────────────────────────────────────────────────────────────────

@config_app.command("reset")
@graceful_errors
def config_reset(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="'group.key' to revert.", metavar="GROUP.KEY"),
) -> None:
    """Revert a config key to its declared default."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.config import reset_user_config_key
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await reset_user_config_key(client, key)

    asyncio.run(_run())


def _coerce_config_value(s: str) -> Any:
    """Mirror Go's `coerceConfigValue`: bool/int/float literals are typed,
    everything else stays a string. The server validates against the schema.
    """
    lowered = s.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s
