"""`cremind channels ...` — register and manage external messaging channels.

Mirrors `cli/cmd/channels.go` for list/catalog/add/delete. The interactive
`pair` command (and the auto-pair behavior of `add`) lands in Phase 4 — for
now `add` registers the channel and prints a hint when interactive pairing
would normally follow.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


channels_app = typer.Typer(
    name="channels",
    help="Register and manage external messaging channels.",
    no_args_is_help=True,
)


@channels_app.command("list")
@graceful_errors
def channels_list(ctx: typer.Context) -> None:
    """List configured channels for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import Channel, list_channels
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[Channel]:
        async with Client(cfg) as client:
            return await list_channels(client)

    channels = asyncio.run(_run())

    if mode.json:
        print_json([c.to_dict() for c in channels])
        return

    table = Table(mode, "ID", "TYPE", "MODE", "AUTH", "REPLY", "ENABLED", "STATUS")
    for c in channels:
        table.add_row(
            c.id,
            c.channel_type,
            c.mode,
            c.auth_mode,
            c.response_mode,
            "true" if c.enabled else "false",
            c.status,
        )
    table.render()


@channels_app.command("catalog")
@graceful_errors
def channels_catalog(ctx: typer.Context) -> None:
    """Print the dynamic channel catalog (TOML-defined)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import get_channel_catalog
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_channel_catalog(client)

    catalog = asyncio.run(_run())

    if mode.json:
        print_json(catalog)
    else:
        sys.stdout.write(_json.dumps(catalog, indent=2, ensure_ascii=False, default=str) + "\n")


@channels_app.command("add")
@graceful_errors
def channels_add(
    ctx: typer.Context,
    channel_type: Optional[str] = typer.Option(
        None, "--type",
        help="Channel type (telegram, whatsapp, discord, messenger, slack, zalo).",
    ),
    mode: str = typer.Option("bot", "--mode", help="Channel mode (bot|userbot|notification)."),
    auth_mode: str = typer.Option("none", "--auth-mode", help="Auth mode (none|otp|password)."),
    response_mode: str = typer.Option("normal", "--response-mode", help="Reply detail (normal|detail)."),
    config_json: Optional[str] = typer.Option(
        None, "--json",
        help="Channel-specific config as JSON; on PowerShell prefer --config.",
    ),
    config_kv: Optional[list[str]] = typer.Option(
        None, "--config",
        help="Channel-specific config as repeatable key=value (alternative to --json).",
    ),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="Start the adapter immediately."),
    no_pair: bool = typer.Option(
        False, "--no-pair",
        help="Skip auto-launching the interactive pairing flow (Phase 4).",
    ),
) -> None:
    """Register a new channel."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import (
        Channel,
        create_channel,
        get_channel_catalog,
    )
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    if not channel_type:
        typer.echo("--type is required (e.g. telegram, discord)", err=True)
        raise typer.Exit(code=1)
    if config_json and config_kv:
        typer.echo("--json and --config are mutually exclusive", err=True)
        raise typer.Exit(code=1)

    config: Optional[dict[str, Any]] = None
    if config_json:
        try:
            parsed = _json.loads(config_json)
        except _json.JSONDecodeError as e:
            typer.echo(
                f"--json: {e}\n"
                "Hint: on Windows PowerShell, inner double-quotes inside --json are stripped\n"
                "      when passing arguments to native binaries. Use --config key=value\n"
                "      instead - e.g. --config phone=+84986664411 (repeat for multiple keys).",
                err=True,
            )
            raise typer.Exit(code=1) from e
        if not isinstance(parsed, dict):
            typer.echo("--json must be an object", err=True)
            raise typer.Exit(code=1)
        config = parsed
    elif config_kv:
        config = {}
        for kv in config_kv:
            if "=" not in kv:
                typer.echo(f"--config '{kv}': expected key=value", err=True)
                raise typer.Exit(code=1)
            k, v = kv.split("=", 1)
            config[k] = v

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> tuple[Channel, dict[str, Any]]:
        async with Client(cfg) as client:
            ch = await create_channel(
                client,
                channel_type=channel_type,
                mode=mode,
                auth_mode=auth_mode,
                response_mode=response_mode,
                enabled=enabled,
                config=config,
            )
            catalog: dict[str, Any] = {}
            if not no_pair and ch.enabled and not out_mode.json:
                # Defensive — fetch catalog only when we might need to hint
                # the user about pairing. JSON mode never auto-pairs.
                try:
                    catalog = await get_channel_catalog(client)
                except Exception:
                    catalog = {}
            return ch, catalog

    channel, catalog = asyncio.run(_run())

    if out_mode.json:
        print_json(channel.to_dict())
        return

    print_kv([
        ("id", channel.id),
        ("channel_type", channel.channel_type),
        ("mode", channel.mode),
        ("auth_mode", channel.auth_mode),
        ("response_mode", channel.response_mode),
        ("enabled", "true" if channel.enabled else "false"),
        ("status", channel.status),
    ])

    if no_pair or not channel.enabled:
        return
    if _channel_mode_needs_pairing(catalog, channel.channel_type, channel.mode):
        sys.stdout.write(
            f"\nThis channel needs interactive pairing.\n"
            f"Run: cremind channels pair {channel.id}\n"
            f"(Auto-pairing on `add` lands in Phase 4 of the migration.)\n"
        )


@channels_app.command("notify-filter")
@graceful_errors
def channels_notify_filter(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id (a notification-mode channel)."),
    filter_json: Optional[str] = typer.Option(
        None, "--json",
        help="New notification filter as JSON; omit to just print the current filter.",
    ),
) -> None:
    """Show or set the notification filter of a notification-mode channel.

    Examples:
      cremind channels notify-filter <id>
      cremind channels notify-filter <id> --json '{"min_priority":"high","source_kinds":["schedule"]}'

    The filter is validated/normalized server-side; setting it restarts the
    adapter so the change takes effect immediately.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import get_channel, set_notification_filter
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    new_filter: Optional[dict[str, Any]] = None
    if filter_json is not None:
        try:
            parsed = _json.loads(filter_json)
        except _json.JSONDecodeError as e:
            typer.echo(f"--json: {e}", err=True)
            raise typer.Exit(code=1) from e
        if not isinstance(parsed, dict):
            typer.echo("--json must be an object", err=True)
            raise typer.Exit(code=1)
        new_filter = parsed

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            if new_filter is not None:
                ch = await set_notification_filter(client, channel_id, new_filter)
            else:
                ch = await get_channel(client, channel_id)
            return ch.to_dict()

    ch = asyncio.run(_run())
    if ch.get("mode") != "notification":
        typer.echo(
            f"Warning: channel mode is {ch.get('mode')!r}, not 'notification' — "
            "the filter only applies in notification mode.",
            err=True,
        )
    current = (ch.get("config") or {}).get("notification_filter") or {}
    if out_mode.json:
        print_json(current)
        return
    typer.echo(_json.dumps(current, indent=2, ensure_ascii=False))


def _parse_config_option(
    config_json: Optional[str], config_kv: Optional[list[str]],
) -> Optional[dict[str, Any]]:
    """Parse mutually-exclusive --json / --config KEY=VALUE into a config dict.

    Mirrors the parsing already used by `channels add`.
    """
    if config_json and config_kv:
        typer.echo("--json and --config are mutually exclusive", err=True)
        raise typer.Exit(code=1)
    if config_json:
        try:
            parsed = _json.loads(config_json)
        except _json.JSONDecodeError as e:
            typer.echo(
                f"--json: {e}\n"
                "Hint: on Windows PowerShell prefer --config key=value.",
                err=True,
            )
            raise typer.Exit(code=1) from e
        if not isinstance(parsed, dict):
            typer.echo("--json must be an object", err=True)
            raise typer.Exit(code=1)
        return parsed
    if config_kv:
        config: dict[str, Any] = {}
        for kv in config_kv:
            if "=" not in kv:
                typer.echo(f"--config '{kv}': expected key=value", err=True)
                raise typer.Exit(code=1)
            k, v = kv.split("=", 1)
            config[k] = v
        return config
    return None


@channels_app.command("edit")
@graceful_errors
def channels_edit(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    mode: Optional[str] = typer.Option(
        None, "--mode", help="Channel mode (bot|userbot|notification).",
    ),
    auth_mode: Optional[str] = typer.Option(
        None, "--auth-mode", help="Auth mode (none|otp|password).",
    ),
    response_mode: Optional[str] = typer.Option(
        None, "--response-mode", help="Reply detail (normal|detail).",
    ),
    config_json: Optional[str] = typer.Option(
        None, "--json", help="Config patch as JSON; on PowerShell prefer --config.",
    ),
    config_kv: Optional[list[str]] = typer.Option(
        None, "--config", help="Config patch as repeatable key=value.",
    ),
) -> None:
    """Update a channel's settings (only the flags you pass).

    `config` is merged server-side, so you can patch one field without
    resending the rest. The main channel cannot be edited.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import update_channel
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    config = _parse_config_option(config_json, config_kv)
    fields: dict[str, Any] = {}
    if mode is not None:
        fields["mode"] = mode
    if auth_mode is not None:
        fields["auth_mode"] = auth_mode
    if response_mode is not None:
        fields["response_mode"] = response_mode
    if config is not None:
        fields["config"] = config
    if not fields:
        typer.echo(
            "nothing to update — pass at least one of --mode / --auth-mode / "
            "--response-mode / --json / --config",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> Any:
        async with Client(cfg) as client:
            return await update_channel(client, channel_id, fields)

    channel = asyncio.run(_run())

    if out_mode.json:
        print_json(channel.to_dict())
        return
    print_kv([
        ("id", channel.id),
        ("channel_type", channel.channel_type),
        ("mode", channel.mode),
        ("auth_mode", channel.auth_mode),
        ("response_mode", channel.response_mode),
        ("enabled", "true" if channel.enabled else "false"),
        ("status", channel.status),
    ])


def _set_channel_enabled(ctx: typer.Context, channel_id: str, enabled: bool) -> None:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import update_channel
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> Any:
        async with Client(cfg) as client:
            return await update_channel(client, channel_id, {"enabled": enabled})

    channel = asyncio.run(_run())
    if out_mode.json:
        print_json(channel.to_dict())
    else:
        state = "true" if channel.enabled else "false"
        sys.stdout.write(f"{channel.id}: enabled={state} status={channel.status}\n")


@channels_app.command("enable")
@graceful_errors
def channels_enable(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
) -> None:
    """Enable a channel (start its adapter)."""
    _set_channel_enabled(ctx, channel_id, True)


@channels_app.command("disable")
@graceful_errors
def channels_disable(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
) -> None:
    """Disable a channel (stop its adapter)."""
    _set_channel_enabled(ctx, channel_id, False)


@channels_app.command("senders")
@graceful_errors
def channels_senders(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
) -> None:
    """List the senders seen on a channel (pending OTP codes are redacted)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import list_senders
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_senders(client, channel_id)

    senders = asyncio.run(_run())

    if mode.json:
        print_json(senders)
        return
    if not senders:
        sys.stdout.write("no senders.\n")
        return
    table = Table(mode, "SENDER_ID", "NAME", "AUTHED", "CONVERSATION_ID", "PENDING_OTP")
    for s in senders:
        table.add_row(
            string_field(s, "sender_id"),
            string_field(s, "display_name"),
            bool_field(s, "authenticated", False),
            string_field(s, "conversation_id"),
            string_field(s, "pending_otp"),
        )
    table.render()


@channels_app.command("pair")
@graceful_errors
def channels_pair(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
) -> None:
    """Interactive pairing flow (QR scan, verification code, 2FA password).

    For WhatsApp this renders the linked-device QR as a Unicode-block code
    (rotates ~every 20s). For Telegram userbot it prompts for the verification
    code and, if 2FA is enabled, the cloud password (typed without echo).

    The command exits when pairing succeeds (`ready`), the session is logged
    out remotely, the server returns a fatal error, or you press Ctrl-C.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import channel_auth_events_path
    from app.cli.config import Config
    from app.cli.output import OutputMode

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            async for event in client.stream(channel_auth_events_path(channel_id)):
                payload = event.data if isinstance(event.data, dict) else {}
                kind = str(payload.get("kind") or "")

                if mode.json:
                    sys.stdout.write(event.raw)
                    if not event.raw.endswith("\n"):
                        sys.stdout.write("\n")
                    sys.stdout.flush()
                    if kind == "ready":
                        return
                    continue

                if kind == "qr":
                    _render_terminal_qr(str(payload.get("raw") or ""))
                elif kind == "code_required":
                    await _prompt_and_submit(
                        client, channel_id, payload, secret=False,
                    )
                elif kind == "password_required":
                    await _prompt_and_submit(
                        client, channel_id, payload, secret=True,
                    )
                elif kind == "ready":
                    sys.stdout.write("\n[OK] Paired successfully.\n")
                    return
                elif kind == "disconnected":
                    if payload.get("logged_out"):
                        sys.stdout.write(
                            "\nSession was logged out - pair again to re-link.\n"
                        )
                        return
                    sys.stdout.write("\nDisconnected - waiting for reconnect...\n")
                elif kind == "error":
                    err = str(payload.get("error") or "")
                    if err:
                        sys.stderr.write(f"error: {err}\n")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


def _render_terminal_qr(raw: str) -> None:
    """Clear the screen and render a QR code using half-block Unicode."""
    from app.cli.io.qr import clear_screen, render_half_block

    if not raw:
        sys.stdout.write("(QR received without raw payload - open the web UI to scan)\n")
        return
    sys.stdout.write(clear_screen())
    sys.stdout.write(
        "Open WhatsApp -> Settings -> Linked Devices -> Link a Device, then scan:\n\n"
    )
    sys.stdout.write(render_half_block(raw) + "\n\n")
    sys.stdout.write(
        "(QR rotates every ~20s; the latest one will replace this view automatically)\n"
    )
    sys.stdout.flush()


async def _prompt_and_submit(
    client,
    channel_id: str,
    payload: dict,
    *,
    secret: bool,
) -> None:
    """Prompt the user for a code or password and POST it back to the server."""
    import asyncio
    import getpass

    from app.cli.client.channels import submit_channel_auth_input

    sys.stdout.write("\n")
    err = str(payload.get("error") or "")
    if err:
        sys.stderr.write(f"error: {err}\n")

    kind = str(payload.get("kind") or "")
    if kind == "code_required":
        phone = str(payload.get("phone") or "")
        hint = phone if phone else "your phone"
        sys.stdout.write(f"Telegram sent a verification code to {hint}.\n")
        prompt = "Code: "
    else:
        sys.stdout.write("Two-step verification password required.\n")
        prompt = "Password: "

    loop = asyncio.get_running_loop()
    try:
        if secret:
            value = await loop.run_in_executor(None, lambda: getpass.getpass(prompt))
        else:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            value = await loop.run_in_executor(None, sys.stdin.readline)
            value = value.rstrip("\r\n")
    except EOFError:
        raise RuntimeError("aborted: stdin closed before input was provided")

    if not value:
        sys.stderr.write("empty input - waiting for next prompt...\n")
        return

    code = value if kind == "code_required" else ""
    password = value if kind != "code_required" else ""
    try:
        await submit_channel_auth_input(client, channel_id, code, password)
    except Exception as e:
        sys.stderr.write(f"submit failed: {e}\n")


@channels_app.command("delete")
@graceful_errors
def channels_delete(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
) -> None:
    """Delete a channel (cascades all its conversations and senders)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import delete_channel
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_channel(client, channel_id)

    asyncio.run(_run())


def _channel_mode_needs_pairing(
    catalog: dict[str, Any],
    channel_type: str,
    mode_id: str,
) -> bool:
    """Mirror Go's `channelModeNeedsPairing`: catalog declares a `setup_kind`
    for the (channel_type, mode) pair when interactive pairing is required.
    """
    entry = catalog.get(channel_type) if isinstance(catalog, dict) else None
    if not isinstance(entry, dict):
        return False
    channel_section = entry.get("channel")
    if not isinstance(channel_section, dict):
        return False
    modes = channel_section.get("modes")
    if not isinstance(modes, list):
        return False
    for m in modes:
        if not isinstance(m, dict):
            continue
        if str(m.get("id") or "") == mode_id:
            return bool(m.get("setup_kind"))
    return False
