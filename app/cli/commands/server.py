"""`cremind server ...` — operate the running Cremind server.

Mirrors the Developer page's operational controls: restart the backend
(with an install-mode-aware confirmation), probe `/health`, and read the
server's build version + tray/install capabilities.

Note the distinction from the root `cremind version` command, which prints the
*locally installed* package version. `cremind server version` reports what the
*connected server* is running — they can differ (e.g. an older Electron pinned
to a newer wheel, or a remote `--server`).

The three read commands are unauthenticated, so they work without a token —
handy for probing a server before login. `server restart` is admin-only.
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from app.cli.commands._helpers import graceful_errors


server_app = typer.Typer(
    name="server",
    help="Operate the running Cremind server (health, version, restart).",
    no_args_is_help=True,
)


# install_mode → the restart caveat the Developer page shows.
_RESTART_CAVEAT = {
    "docker": "Docker install — the container will restart automatically "
              "(usually 5-15 seconds).",
    "electron": "Electron install — Cremind will relaunch the backend "
                "automatically.",
}
_RESTART_CAVEAT_DEFAULT = (
    "No supervisor detected — the backend will stay DOWN after it stops. "
    "You will need to relaunch `cremind serve` manually."
)


@server_app.command("health")
@graceful_errors
def server_health(ctx: typer.Context) -> None:
    """Probe the server's /health endpoint (no token required).

    Exits non-zero when the server reports a degraded subsystem (HTTP 503);
    a `disabled` vector store is healthy, not an error.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_health
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> tuple[int, Any]:
        async with Client(cfg) as client:
            return await get_health(client)

    status_code, body = asyncio.run(_run())
    body = body if isinstance(body, dict) else {}

    if mode.json:
        print_json(body)
    else:
        print_kv([
            ("status", str(body.get("status") or "")),
            ("db", str(body.get("db") or "")),
            ("vectorstore", str(body.get("vectorstore") or "")),
        ])

    if status_code >= 400:
        raise typer.Exit(code=1)


@server_app.command("version")
@graceful_errors
def server_version(ctx: typer.Context) -> None:
    """Show the connected server's build version and release channel.

    Distinct from `cremind version`, which prints the locally installed CLI
    package version.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_server_version
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_server_version(client)

    info = asyncio.run(_run())

    if mode.json:
        print_json(info)
        return
    print_kv([
        ("backend", str(info.get("backend") or "")),
        ("schema", str(info.get("schema") or "")),
        ("channel", str(info.get("channel") or "")),
        ("min_supported_upgrade_from", str(info.get("min_supported_upgrade_from") or "")),
    ])


@server_app.command("capabilities")
@graceful_errors
def server_capabilities(ctx: typer.Context) -> None:
    """Show the server's install mode and the UI features it exposes.

    Reads the public tray-capabilities endpoint. The richer admin
    `/api/services/capabilities` (per-service deployment modes) is
    Setup-Wizard-scoped and intentionally not exposed here.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_tray_capabilities
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tray_capabilities(client)

    caps = asyncio.run(_run())

    if mode.json:
        print_json(caps)
        return
    ui_features = caps.get("ui_features")
    features_str = ", ".join(ui_features) if isinstance(ui_features, list) else ""
    print_kv([
        ("install_mode", str(caps.get("install_mode") or "")),
        ("ui_features", features_str),
    ])


@server_app.command("restart")
@graceful_errors
def server_restart(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Restart the backend process (admin).

    Active HTTP, SSE, and chat connections drop while the server is
    unavailable. Whether it comes back on its own depends on the install
    mode — this command reads it and warns accordingly before confirming.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_tray_capabilities, restart_server
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _capabilities() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tray_capabilities(client)

    caps = asyncio.run(_capabilities())
    install_mode = str(caps.get("install_mode") or "")
    caveat = _RESTART_CAVEAT.get(install_mode, _RESTART_CAVEAT_DEFAULT)

    # Caveat/prompt go to stderr so stdout stays clean for --json.
    if not yes:
        sys.stderr.write(caveat + "\n")
        if not typer.confirm("Restart the Cremind server now?", default=False):
            raise typer.Exit(code=1)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await restart_server(client)

    result = asyncio.run(_run())

    if mode.json:
        print_json(result)
        return
    pid = string_field(result, "pid")
    sys.stdout.write(f"restarting{f' (pid {pid})' if pid else ''}\n")
