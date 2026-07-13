"""`cremind embedding ...` — inspect and control the vector-embedding subsystem.

Mirrors the admin-only Embedding Settings page: read live status, get or set the
persisted embedding config (provider + vector store), kick off an
initialize/rebuild, and optionally --follow the load progress over SSE.

Setting a provider whose optional extras aren't installed returns a
`FeatureNotInstalled` error listing the missing feature keys — install them with
`cremind features install <key>` first.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


embedding_app = typer.Typer(
    name="embedding",
    help="Inspect and control the vector-embedding subsystem (admin).",
    no_args_is_help=True,
)


@embedding_app.command("status")
@graceful_errors
def embedding_status(
    ctx: typer.Context,
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Tail the live embedding state stream (SSE) until interrupted.",
    ),
) -> None:
    """Show the embedding subsystem's current state (or --follow it live)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.embedding import embedding_stream_path, get_status
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    if follow:
        async def _tail() -> None:
            async with Client(cfg) as client:
                await run_admin_stream(client, embedding_stream_path(), mode)

        try:
            asyncio.run(_tail())
        except KeyboardInterrupt:
            raise typer.Exit(code=130)
        return

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_status(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        print_map(out)


@embedding_app.command("get")
@graceful_errors
def embedding_get(ctx: typer.Context) -> None:
    """Print the persisted embedding config (admin)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.embedding import get_config
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_config(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")


@embedding_app.command("set")
@graceful_errors
def embedding_set(
    ctx: typer.Context,
    config_json: Optional[str] = typer.Option(
        None, "--json",
        help="Embedding config as JSON; on PowerShell prefer --file.",
    ),
    config_file: Optional[str] = typer.Option(
        None, "--file",
        help="Path to a JSON file with the embedding config (avoids shell quoting).",
    ),
) -> None:
    """Persist a new embedding config and trigger a reload/rebuild (admin).

    Body mirrors the wizard's embedding_config (e.g. {"enabled": true,
    "provider": "me5", "vectorstore": {...}}).
    """
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.config import Config
    from app.cli.client.embedding import put_config
    from app.cli.output import OutputMode, print_json, print_map

    if config_json and config_file:
        typer.echo("--json and --file are mutually exclusive", err=True)
        raise typer.Exit(code=1)
    if not config_json and not config_file:
        typer.echo("provide the config via --json or --file", err=True)
        raise typer.Exit(code=1)

    raw = config_json
    if config_file:
        try:
            with open(config_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as e:
            typer.echo(f"--file: {e}", err=True)
            raise typer.Exit(code=1) from e

    try:
        parsed = _json.loads(raw or "")
    except _json.JSONDecodeError as e:
        typer.echo(f"config is not valid JSON: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not isinstance(parsed, dict):
        typer.echo("config must be a JSON object", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await put_config(client, parsed)

    try:
        out = asyncio.run(_run())
    except APIError as e:
        detail: Any = None
        if e.raw:
            try:
                detail = _json.loads(e.raw)
            except (ValueError, TypeError):
                detail = None
        if isinstance(detail, dict) and detail.get("error") == "FeatureNotInstalled":
            keys = [
                str(m.get("feature_key"))
                for m in (detail.get("missing") or [])
                if isinstance(m, dict) and m.get("feature_key")
            ]
            sys.stderr.write((detail.get("message") or "Required features are not installed.") + "\n")
            if keys:
                sys.stderr.write(
                    "Install them first: cremind features install " + " ".join(keys) + "\n"
                )
            raise typer.Exit(code=1) from e
        raise

    if mode.json:
        print_json(out)
    else:
        print_map(out)


@embedding_app.command("initialize")
@graceful_errors
def embedding_initialize(
    ctx: typer.Context,
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Tail the embedding state stream (SSE) after kicking off the rebuild.",
    ),
) -> None:
    """Trigger an asynchronous load + rebuild of the embedding subsystem."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.embedding import embedding_stream_path, initialize
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await initialize(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        print_map(out)

    if follow:
        async def _tail() -> None:
            async with Client(cfg) as client:
                await run_admin_stream(client, embedding_stream_path(), mode)

        try:
            asyncio.run(_tail())
        except KeyboardInterrupt:
            raise typer.Exit(code=130)
