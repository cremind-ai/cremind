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
    auth_mode: str = typer.Option(
        "none", "--auth-mode",
        help="Legacy access auth (none|otp|password). Prefer the unified "
             "--config subscribe_auth=open|passcode|otp|approval|allowlist.",
    ),
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


@channels_app.command("send")
@graceful_errors
def channels_send(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id (a notification-mode channel)."),
    message: Optional[str] = typer.Argument(
        None, help="Message text. Omit to read from --message-file or stdin.",
    ),
    message_file: Optional[str] = typer.Option(
        None, "--message-file", "-f",
        help="Read the message from this file (use '-' for stdin). Preferred on "
             "PowerShell, where inline quoting mangles apostrophes/quotes.",
    ),
) -> None:
    """Push an ad-hoc message OUT to a notification-mode channel.

    Delivers to the channel's recipients (configured target chat IDs plus
    everyone who has /start-subscribed) via the running adapter, bypassing the
    channel's notification filter. The channel must be in notification mode and
    its adapter must be running.

    Examples:
      cremind channels send <id> "Deploy finished OK"
      cremind channels send <id> --message-file note.txt
      echo "1+1 = 2" | cremind channels send <id> -f -
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import notify_channel
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    if message is not None and message_file is not None:
        typer.echo("pass either a message argument or --message-file, not both", err=True)
        raise typer.Exit(code=1)

    text: str
    if message_file is not None:
        if message_file == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(message_file, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                typer.echo(f"--message-file: {e}", err=True)
                raise typer.Exit(code=1) from e
    elif message is not None:
        text = message
    else:
        # No message given anywhere — fall back to stdin (supports piping).
        text = sys.stdin.read()

    text = text.strip()
    if not text:
        typer.echo("message is empty — nothing to send", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await notify_channel(client, channel_id, text)

    result = asyncio.run(_run())

    if out_mode.json:
        print_json(result)
        return
    recipients = int(result.get("recipients") or 0)
    if result.get("delivered"):
        sys.stdout.write(f"Delivered to {recipients} recipient(s).\n")
    else:
        sys.stdout.write(
            "Not delivered — the channel has no recipients yet "
            "(ask subscribers to /start, or set target chat IDs).\n"
        )


@channels_app.command("message")
@graceful_errors
def channels_message(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id to send through."),
    message: Optional[str] = typer.Argument(
        None, help="Shared message text. Omit to read from --message-file or stdin.",
    ),
    to: Optional[list[str]] = typer.Option(
        None, "--to",
        help="Recipient: platform sender id or international phone number. "
             "Repeat for several recipients.",
    ),
    recipients_file: Optional[str] = typer.Option(
        None, "--recipients-file",
        help="Read recipients from a JSON file (use '-' for stdin): a list of "
             'strings, or of objects {"to", "message"?, "name"?}. Use this for '
             "per-recipient personalisation.",
    ),
    message_file: Optional[str] = typer.Option(
        None, "--message-file", "-f",
        help="Read the shared message from this file (use '-' for stdin). "
             "Preferred on PowerShell, where inline quoting mangles apostrophes.",
    ),
    country_code: Optional[str] = typer.Option(
        None, "--country-code",
        help='Country code (e.g. "84") used to expand numbers written in '
             "national form with a leading 0.",
    ),
    send: bool = typer.Option(
        False, "--send",
        help="Actually deliver. Without this the command only previews who "
             "would be messaged.",
    ),
) -> None:
    """Message specific clients on a channel — one or many.

    Unlike `channels send` (which broadcasts to a notification channel's own
    subscribers), this addresses named individuals by platform sender id or
    phone number, and records each delivered message in that client's
    conversation so the agent sees it later.

    Previews by default: it resolves every recipient and prints who would be
    messaged, who has never been contacted before, and what failed to resolve.
    Add --send to deliver. Only WhatsApp can message someone who has never
    written first.

    Examples:
      cremind channels message <id> "Thanks for trying our product!" --to +84901234567
      cremind channels message <id> "Thanks!" --to +84901234567 --to +84907654321 --send
      cremind channels message <id> --recipients-file thankyou.json --send
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import send_channel_message
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    if to and recipients_file:
        typer.echo("pass either --to or --recipients-file, not both", err=True)
        raise typer.Exit(code=1)
    if message is not None and message_file is not None:
        typer.echo("pass either a message argument or --message-file, not both", err=True)
        raise typer.Exit(code=1)

    recipients: list[dict[str, Any]] = []
    if recipients_file is not None:
        raw = sys.stdin.read() if recipients_file == "-" else None
        if raw is None:
            try:
                with open(recipients_file, encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as e:
                typer.echo(f"--recipients-file: {e}", err=True)
                raise typer.Exit(code=1) from e
        try:
            parsed = _json.loads(raw)
        except ValueError as e:
            typer.echo(f"--recipients-file: invalid JSON: {e}", err=True)
            raise typer.Exit(code=1) from e
        if not isinstance(parsed, list):
            typer.echo("--recipients-file: expected a JSON list", err=True)
            raise typer.Exit(code=1)
        for item in parsed:
            if isinstance(item, str):
                recipients.append({"to": item})
            elif isinstance(item, dict):
                recipients.append(item)
            else:
                typer.echo(
                    "--recipients-file: each entry must be a string or an object",
                    err=True,
                )
                raise typer.Exit(code=1)
    elif to:
        recipients = [{"to": t} for t in to]
    else:
        typer.echo("give recipients with --to or --recipients-file", err=True)
        raise typer.Exit(code=1)

    text: Optional[str]
    if message_file is not None:
        if message_file == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(message_file, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                typer.echo(f"--message-file: {e}", err=True)
                raise typer.Exit(code=1) from e
    else:
        text = message
    text = text.strip() if text else None
    # A shared message is optional only when every recipient brings its own.
    if not text and not all(r.get("message") for r in recipients):
        typer.echo(
            "no message text — pass one as an argument, via --message-file, or "
            "on every recipient in --recipients-file",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await send_channel_message(
                client, channel_id, recipients, message=text,
                dry_run=not send, default_country_code=country_code,
            )

    result = asyncio.run(_run())

    if out_mode.json:
        print_json(result)
        raise typer.Exit(code=1 if int(result.get("failed") or 0) else 0)

    rows = result.get("results") or []
    table = Table(out_mode, "TO", "STATUS", "CHANNEL", "SENDER_ID", "NEW", "DETAIL")
    for r in rows:
        table.add_row(
            string_field(r, "to"),
            string_field(r, "status"),
            string_field(r, "channel_type"),
            string_field(r, "sender_id"),
            "yes" if r.get("new_contact") else "",
            string_field(r, "detail") or string_field(r, "error"),
        )
    table.render()

    failed = int(result.get("failed") or 0)
    if result.get("dry_run"):
        sys.stdout.write(
            f"\nPreview only — nothing sent. {result.get('resolved') or 0} of "
            f"{len(rows)} recipient(s) resolved"
            + (f", {result['new_contacts']} never contacted before"
               if result.get("new_contacts") else "")
            + ". Re-run with --send to deliver.\n"
        )
    else:
        sys.stdout.write(
            f"\nSent {result.get('sent') or 0}, failed {failed}.\n"
        )
    if result.get("aborted"):
        sys.stdout.write(
            "Aborted early after repeated delivery failures on this channel.\n"
        )
    if failed:
        raise typer.Exit(code=1)


@channels_app.command("set-phone")
@graceful_errors
def channels_set_phone(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    sender_id: str = typer.Argument(..., help="Sender id (from `channels senders`)."),
    phone: Optional[str] = typer.Argument(
        None, help="Phone number in international form, e.g. +84901234567.",
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the stored number."),
) -> None:
    """Record a contact's phone number so `channels message` can reach them by it.

    WhatsApp contacts get this automatically (their sender id is the number);
    everywhere else the mapping has to come from you. This is also the only way
    to *correct* a stored number — automatic derivation never overwrites one.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import set_sender_phone
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    if clear and phone:
        typer.echo("pass either a phone number or --clear, not both", err=True)
        raise typer.Exit(code=1)
    if not clear and not phone:
        typer.echo("give a phone number, or --clear to remove it", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await set_sender_phone(
                client, channel_id, sender_id, None if clear else phone,
            )

    sender = asyncio.run(_run())
    if out_mode.json:
        print_json(sender)
        return
    stored = sender.get("phone")
    sys.stdout.write(
        f"{sender.get('sender_id')}: phone "
        + (f"set to {stored}\n" if stored else "cleared\n")
    )


@channels_app.command("set-confirm")
@graceful_errors
def channels_set_confirm(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    sender_id: str = typer.Argument(..., help="Sender id (from `channels senders`)."),
    mode: str = typer.Argument(
        ...,
        help="'default' (inherit the profile setting), 'always' (always ask "
             "before messaging this client), or 'never' (send directly).",
    ),
) -> None:
    """Choose whether the agent must ask before messaging one client.

    The profile-wide default lives in Settings → Config → Channels ("Confirm
    before messaging clients", `cremind config set channels.confirm_before_send`).
    This overrides it for a single client:

      never   — the agent messages them without stopping to ask. This is what
                lets an unattended automation reach a pre-approved client
                instead of stalling on a prompt nobody can answer.
      always  — keep asking for this client even if the profile setting is off.
      default — clear the override and inherit the profile setting.

    Someone who has never messaged the channel always prompts, whatever is set.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import set_sender_confirmation
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    wanted = (mode or "").strip().lower()
    choices = {"default": None, "always": "required", "never": "skip"}
    if wanted not in choices:
        typer.echo(
            f"mode must be one of {', '.join(choices)} (got {mode!r})", err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await set_sender_confirmation(
                client, channel_id, sender_id, choices[wanted],
            )

    sender = asyncio.run(_run())
    if out_mode.json:
        print_json(sender)
        return
    stored = sender.get("send_confirmation")
    label = {"required": "always ask", "skip": "send directly"}.get(
        stored or "", "inherit the profile setting",
    )
    sys.stdout.write(f"{sender.get('sender_id')}: {label}\n")


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
        None, "--auth-mode",
        help="Legacy access auth (none|otp|password). Prefer the unified "
             "--config subscribe_auth=open|passcode|otp|approval|allowlist.",
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
    table = Table(
        mode, "SENDER_ID", "NAME", "PHONE", "AUTHED", "CONFIRM", "TOKENS",
        "COST_USD", "CONVERSATION_ID", "PENDING_OTP",
    )
    for s in senders:
        usage = s.get("usage") or {}
        tokens = usage.get("total_tokens")
        cost = usage.get("total_usd")
        table.add_row(
            string_field(s, "sender_id"),
            string_field(s, "display_name"),
            string_field(s, "phone"),
            bool_field(s, "authenticated", False),
            # Blank means "inherit the profile setting" — the common case.
            {"required": "always", "skip": "never"}.get(
                s.get("send_confirmation") or "", "",
            ),
            f"{int(tokens):,}" if isinstance(tokens, (int, float)) else "",
            f"{float(cost):.4f}" if isinstance(cost, (int, float)) else "",
            string_field(s, "conversation_id"),
            string_field(s, "pending_otp"),
        )
    table.render()


def _set_sender_authenticated(
    ctx: typer.Context, channel_id: str, sender_id: str, authenticated: bool,
) -> None:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import set_sender_authenticated
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await set_sender_authenticated(
                client, channel_id, sender_id, authenticated,
            )

    sender = asyncio.run(_run())
    if out_mode.json:
        print_json(sender)
        return
    state = "approved" if sender.get("authenticated") else "revoked"
    sys.stdout.write(
        f"{sender.get('sender_id')}: {state} on channel {channel_id}\n"
    )


@channels_app.command("approve")
@graceful_errors
def channels_approve(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id (a notification-mode channel)."),
    sender_id: str = typer.Argument(..., help="Sender id to approve (from `channels senders`)."),
) -> None:
    """Approve a pending subscriber on an `approval`-auth notification channel.

    The subscriber must have contacted the channel first (sent /start); find
    their id with `cremind channels senders <channel_id>`.
    """
    _set_sender_authenticated(ctx, channel_id, sender_id, True)


@channels_app.command("revoke")
@graceful_errors
def channels_revoke(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    sender_id: str = typer.Argument(..., help="Sender id to revoke (from `channels senders`)."),
) -> None:
    """Revoke a subscriber so they stop receiving notifications."""
    _set_sender_authenticated(ctx, channel_id, sender_id, False)


@channels_app.command("clear-history")
@graceful_errors
def channels_clear_history(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    sender_id: str = typer.Argument(..., help="Sender id (from `channels senders`)."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Delete every message in one subscriber's conversation.

    The conversation itself is kept: the subscriber's next message continues in
    it, and their token/cost totals in `channels senders` survive the wipe.
    Fails with a 409 while that subscriber has a run in progress.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import clear_sender_history
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if not yes:
        prompt = (
            f"Delete all messages in {sender_id}'s conversation on channel "
            f"{channel_id}? (usage totals are kept)"
        )
        if not sys.stdin.isatty():
            # Non-interactive (scripts, exec_shell): never guess on a
            # destructive action — make the caller opt in explicitly.
            typer.echo(
                f"{prompt} Re-run with --yes to confirm.", err=True,
            )
            raise typer.Exit(code=1)
        if not typer.confirm(prompt):
            raise typer.Exit(code=1)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await clear_sender_history(client, channel_id, sender_id)

    result = asyncio.run(_run())
    if out_mode.json:
        print_json(result)
        return
    cleared = result.get("cleared_messages", 0)
    conv = result.get("conversation_id")
    if not conv:
        sys.stdout.write(f"{sender_id}: no conversation to clear\n")
        return
    sys.stdout.write(
        f"{sender_id}: cleared {cleared} message(s) from conversation {conv}\n"
    )


@channels_app.command("forget")
@graceful_errors
def channels_forget(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    sender_id: str = typer.Argument(..., help="Sender id (from `channels senders`)."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Delete a channel client completely — as if they had never messaged.

    Removes their conversation and every message in it, the automations homed on
    it, their contact details and phone, and their access approval. If they write
    again they arrive as a brand-new client and must pass the channel's access
    check from scratch.

    Recorded token usage and cost stay in the account totals but stop being
    attributed to anyone. Fails with a 409 while that client has a run in
    progress. This cannot be undone — use `clear-history` instead to wipe only
    their messages and keep the person.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import delete_sender
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if not yes:
        prompt = (
            f"Completely delete client {sender_id} from channel {channel_id}? "
            "Their conversation, messages, automations, contact details and "
            "access approval are all removed. This cannot be undone."
        )
        if not sys.stdin.isatty():
            # Non-interactive (scripts, exec_shell): never guess on a
            # destructive action — make the caller opt in explicitly.
            typer.echo(f"{prompt} Re-run with --yes to confirm.", err=True)
            raise typer.Exit(code=1)
        if not typer.confirm(prompt):
            raise typer.Exit(code=1)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await delete_sender(client, channel_id, sender_id)

    result = asyncio.run(_run())
    if out_mode.json:
        print_json(result)
        return
    deleted = result.get("deleted_messages", 0)
    sys.stdout.write(f"{sender_id}: deleted from channel {channel_id}\n")
    if deleted:
        sys.stdout.write(f"  removed {deleted} message(s)\n")
    if result.get("forgot_memories"):
        sys.stdout.write(
            f"  forgot {result['forgot_memories']} long-term memory entr"
            f"{'y' if result['forgot_memories'] == 1 else 'ies'}\n"
        )
    if result.get("unsubscribed_target"):
        sys.stdout.write("  removed from the channel's target chat IDs\n")


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
