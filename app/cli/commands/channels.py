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


def _read_attachment_files(paths: Optional[list[str]]) -> list[tuple[str, bytes]]:
    """Read ``--file`` attachments into ``(basename, bytes)`` pairs.

    Read client-side because the CLI may run on a different machine — its
    paths mean nothing to the server, so the bytes travel as multipart.
    Errors exit cleanly before any request is made.
    """
    import os

    out: list[tuple[str, bytes]] = []
    for path in paths or []:
        try:
            with open(path, "rb") as fh:
                out.append((os.path.basename(path), fh.read()))
        except OSError as e:
            typer.echo(f"--file: {e}", err=True)
            raise typer.Exit(code=1) from e
    return out


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
    group_chats: bool = typer.Option(
        False, "--group-chats/--no-group-chats",
        help="Let this channel's agent take part in platform group chats. New "
             "groups arrive as pending and must be approved with "
             "`channels groups approve` (default: off).",
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

    if group_chats:
        # A real boolean, not the "true" string a --config key would carry: the
        # server validates the type and would refuse the string.
        config = {**(config or {}), "group_chats_enabled": True}

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
    file: Optional[list[str]] = typer.Option(
        None, "--file", "-F",
        help="Attach a local file to the send. Repeat for several files. "
             "Recipients on a platform that can't carry files get a text "
             "notice naming the file instead.",
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
      cremind channels send <id> "monthly report attached" -F report.pdf
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
    elif file:
        # Attachments alone are a complete send; don't park on stdin for a
        # message nobody intends to type.
        text = ""
    else:
        # No message given anywhere — fall back to stdin (supports piping).
        text = sys.stdin.read()

    text = text.strip()
    if not text and not file:
        typer.echo("message is empty — nothing to send", err=True)
        raise typer.Exit(code=1)

    files = _read_attachment_files(file)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await notify_channel(client, channel_id, text, files=files)

    result = asyncio.run(_run())

    if out_mode.json:
        print_json(result)
        return
    recipients = int(result.get("recipients") or 0)
    if result.get("delivered"):
        suffix = ""
        if files:
            suffix = f" ({int(result.get('files_delivered') or 0)} file(s))"
        sys.stdout.write(f"Delivered to {recipients} recipient(s){suffix}.\n")
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
    file: Optional[list[str]] = typer.Option(
        None, "--file", "-F",
        help="Attach a local file, delivered to every recipient after their "
             "text. Repeat for several files.",
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
    # A shared message is optional when every recipient brings its own, or
    # when the send is carried by attachments alone.
    if not text and not file and not all(r.get("message") for r in recipients):
        typer.echo(
            "no message text — pass one as an argument, via --message-file, or "
            "on every recipient in --recipients-file",
            err=True,
        )
        raise typer.Exit(code=1)

    files = _read_attachment_files(file)

    cfg: Config = ctx.obj["cfg"]
    out_mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await send_channel_message(
                client, channel_id, recipients, message=text,
                dry_run=not send, default_country_code=country_code,
                files=files,
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
    group_chats: Optional[bool] = typer.Option(
        None, "--group-chats/--no-group-chats",
        help="Whether this channel's agent takes part in platform group chats. "
             "Turning it off leaves the groups on record but stops the agent "
             "reading them.",
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
    if group_chats is not None:
        # Merged into the same patch rather than sent separately: the server
        # merges ``config`` one level deep, so a flag and a --config key set in
        # one call must arrive as one object.
        config = {**(config or {}), "group_chats_enabled": group_chats}
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
            "--response-mode / --group-chats / --json / --config",
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


# ── channel group chats ───────────────────────────────────────────────────
#
# A platform group this channel's own account is in — a Telegram supergroup, a
# Slack channel, a WhatsApp group. Not `cremind group`, which is Cremind's own
# rooms where several profiles' agents talk to each other; the two are separate
# features and share nothing.


groups_app = typer.Typer(
    name="groups",
    help="Approve and manage the platform groups this channel's agent is in.",
    no_args_is_help=True,
)
channels_app.add_typer(groups_app, name="groups")


def _run_group_async(coro: Any) -> Any:
    """`asyncio.run` plus a clean exit for the group resolver's `RuntimeError`.

    `graceful_errors` only covers config/API/network failures, so without this a
    mistyped group name would end in a traceback instead of one line of advice.
    """
    import asyncio

    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e


def _stamp(value: Any) -> str:
    """Render an epoch-MILLISECONDS timestamp; channel-group rows store ms.

    ``epoch_seconds_field`` is the shared formatter and takes seconds, so the
    conversion happens here rather than being silently wrong by a factor of a
    thousand.
    """
    from app.cli.output.formatting import epoch_seconds_field

    if not value:
        return ""
    try:
        return epoch_seconds_field(float(value) / 1000.0)
    except (TypeError, ValueError):
        return ""


def _policy_of(group: dict[str, Any]) -> dict[str, Any]:
    policy = (group.get("settings") or {}).get("member_policy") or {}
    return {
        "mode": str(policy.get("mode") or "everyone"),
        "allow": list(policy.get("allow") or []),
        "deny": list(policy.get("deny") or []),
    }


@groups_app.command("list")
@graceful_errors
def channel_groups_list(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id (from `channels list`)."),
    status: Optional[str] = typer.Option(
        None, "--status", help="Only pending, approved or blocked groups.",
    ),
) -> None:
    """List the platform groups this channel's account has been added to.

    A group appears here the moment the account is added to it (or, on platforms
    that report no join, when somebody first speaks). It stays `pending` — and
    the agent stays deaf to it — until you `approve` it.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.channels import list_channel_groups
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import epoch_seconds_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_channel_groups(client, channel_id, status or "")

    groups = asyncio.run(_run())

    if mode.json:
        print_json(groups)
        return
    if not groups:
        sys.stdout.write(
            "no groups seen yet — add this channel's account to a group, and "
            "make sure group chats are enabled for the channel "
            f"(`cremind channels edit {channel_id} --group-chats`).\n"
        )
        return
    table = Table(
        mode, "GROUP_ID", "CHAT_ID", "TITLE", "STATUS", "MEMBERS", "POLICY",
        "LAST_MESSAGE",
    )
    for group in groups:
        policy = _policy_of(group)
        table.add_row(
            str(group.get("id") or ""),
            str(group.get("platform_chat_id") or ""),
            str(group.get("title") or ""),
            str(group.get("status") or ""),
            str(group.get("member_count") or 0),
            policy["mode"],
            _stamp(group.get("last_message_at")),
        )
    table.render()


def _set_group_status(
    ctx: typer.Context, channel_id: str, group: str, status: str,
) -> None:
    from app.cli.client._base import Client
    from app.cli.client.channels import (
        resolve_channel_group,
        set_channel_group_status,
    )
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            found = await resolve_channel_group(client, channel_id, group)
            return await set_channel_group_status(
                client, channel_id, str(found["id"]), status,
            )

    updated = _run_group_async(_run())

    if mode.json:
        print_json(updated)
        return
    print_kv([
        ("id", str(updated.get("id") or "")),
        ("title", str(updated.get("title") or "")),
        ("status", str(updated.get("status") or "")),
        ("conversation_id", str(updated.get("conversation_id") or "")),
    ])


@groups_app.command("approve")
@graceful_errors
def channel_groups_approve(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
) -> None:
    """Let the agent take part in this group.

    From now on the agent reads the group's messages and replies when it is
    addressed — or, for a message that does not mention it, when a cheap
    relevance check says the message is for it. Who it may answer is the group's
    member policy (`groups policy`, `groups allow`, `groups deny`).
    """
    _set_group_status(ctx, channel_id, group, "approved")


@groups_app.command("block")
@graceful_errors
def channel_groups_block(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
) -> None:
    """Stop the agent taking part in this group, and remember the decision.

    The transcript so far is kept, and being added to the group again does not
    ask you a second time. Use `groups forget` to erase it instead.
    """
    _set_group_status(ctx, channel_id, group, "blocked")


@groups_app.command("forget")
@graceful_errors
def channel_groups_forget(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Erase a group and its transcript, as if the account had never been in it.

    Not the same as `block`, which is a decision on the record. After forgetting,
    the next message from that group asks you to approve it again. Fails with a
    409 while the group has a run in progress. This cannot be undone.
    """
    from app.cli.client._base import Client
    from app.cli.client.channels import delete_channel_group, resolve_channel_group
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if not yes:
        prompt = (
            f"Forget group {group} on channel {channel_id}? Its conversation and "
            "every message in it are removed, and being added again will ask you "
            "to approve it afresh. This cannot be undone."
        )
        if not sys.stdin.isatty():
            # Non-interactive (scripts, exec_shell): never guess on a
            # destructive action — make the caller opt in explicitly.
            typer.echo(f"{prompt} Re-run with --yes to confirm.", err=True)
            raise typer.Exit(code=1)
        if not typer.confirm(prompt):
            typer.echo("aborted", err=True)
            raise typer.Exit(code=1)

    async def _run() -> str:
        async with Client(cfg) as client:
            found = await resolve_channel_group(client, channel_id, group)
            await delete_channel_group(client, channel_id, str(found["id"]))
            return str(found["id"])

    group_id = _run_group_async(_run())

    if mode.json:
        print_json({"deleted": True, "group_id": group_id})
        return
    sys.stdout.write(f"{group_id}: forgotten\n")


@groups_app.command("members")
@graceful_errors
def channel_groups_members(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
) -> None:
    """Who is in a group, and whether the agent answers them.

    `SOURCE` says where each row came from: `roster` is the platform's own
    member list, `seen` is somebody who has posted. Some platforms name nobody —
    a Telegram bot can only list administrators, and a Zalo bot not even those —
    so a short list is not necessarily a wrong one.
    """
    from app.cli.client._base import Client
    from app.cli.client.channels import resolve_channel_group
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import epoch_seconds_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await resolve_channel_group(client, channel_id, group)

    found = _run_group_async(_run())
    members = found.get("members") or []

    if mode.json:
        print_json(members)
        return
    if not members:
        sys.stdout.write(
            "nobody recorded yet — this platform may not name group members; "
            "they appear here as they post.\n"
        )
        return
    table = Table(mode, "MEMBER_ID", "NAME", "SOURCE", "BOT", "RESPONDS", "LAST_SEEN")
    for member in members:
        table.add_row(
            str(member.get("member_id") or ""),
            str(member.get("display_name") or member.get("username") or ""),
            str(member.get("source") or ""),
            "true" if member.get("is_bot") else "false",
            "true" if member.get("responds") else "false",
            _stamp(member.get("last_seen_at")),
        )
    table.render()


def _patch_group_settings(
    ctx: typer.Context, channel_id: str, group: str, build: Any,
) -> None:
    """Resolve a group, build a settings patch from its current one, send it."""
    from app.cli.client._base import Client
    from app.cli.client.channels import (
        resolve_channel_group,
        set_channel_group_settings,
    )
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            found = await resolve_channel_group(client, channel_id, group)
            return await set_channel_group_settings(
                client, channel_id, str(found["id"]), build(found),
            )

    updated = _run_group_async(_run())

    if mode.json:
        print_json(updated)
        return
    policy = _policy_of(updated)
    settings = updated.get("settings") or {}
    print_kv([
        ("id", str(updated.get("id") or "")),
        ("title", str(updated.get("title") or "")),
        ("respond_mode", str(settings.get("respond_mode") or "")),
        ("policy_mode", policy["mode"]),
        ("allow", ", ".join(policy["allow"])),
        ("deny", ", ".join(policy["deny"])),
    ])


@groups_app.command("policy")
@graceful_errors
def channel_groups_policy(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
    policy_mode: str = typer.Argument(
        ..., metavar="MODE", help="everyone | selected",
    ),
) -> None:
    """Choose who the agent answers: everyone, or only the accounts you allow.

    `everyone` answers anybody in the group except those on the deny list;
    `selected` answers only the allow list. Both lists are kept when you switch,
    so flipping back does not lose one you curated.
    """
    if policy_mode not in ("everyone", "selected"):
        typer.echo("MODE must be 'everyone' or 'selected'", err=True)
        raise typer.Exit(code=1)

    def build(found: dict[str, Any]) -> dict[str, Any]:
        policy = _policy_of(found)
        policy["mode"] = policy_mode
        return {"member_policy": policy}

    _patch_group_settings(ctx, channel_id, group, build)


@groups_app.command("allow")
@graceful_errors
def channel_groups_allow(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
    member_id: list[str] = typer.Argument(
        ..., help="Member ids (from `channels groups members`).",
    ),
) -> None:
    """Answer these accounts: add them to the allow list, off the deny list."""
    wanted = [m.strip() for m in member_id if m.strip()]

    def build(found: dict[str, Any]) -> dict[str, Any]:
        policy = _policy_of(found)
        policy["allow"] = list(dict.fromkeys([*policy["allow"], *wanted]))
        policy["deny"] = [d for d in policy["deny"] if d not in wanted]
        return {"member_policy": policy}

    _patch_group_settings(ctx, channel_id, group, build)


@groups_app.command("deny")
@graceful_errors
def channel_groups_deny(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
    member_id: list[str] = typer.Argument(
        ..., help="Member ids (from `channels groups members`).",
    ),
) -> None:
    """Never answer these accounts: add them to the deny list, off the allow list.

    Their messages are dropped outright rather than kept as context, so a denied
    account cannot fill the agent's history either.
    """
    wanted = [m.strip() for m in member_id if m.strip()]

    def build(found: dict[str, Any]) -> dict[str, Any]:
        policy = _policy_of(found)
        policy["deny"] = list(dict.fromkeys([*policy["deny"], *wanted]))
        policy["allow"] = [a for a in policy["allow"] if a not in wanted]
        return {"member_policy": policy}

    _patch_group_settings(ctx, channel_id, group, build)


@groups_app.command("respond")
@graceful_errors
def channel_groups_respond(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
    respond_mode: str = typer.Argument(
        ..., metavar="MODE", help="mention_or_relevant | mention_only",
    ),
) -> None:
    """When the agent may speak without being mentioned.

    `mention_or_relevant` (the default) runs a cheap relevance check on messages
    that do not mention the agent and replies when the answer is yes.
    `mention_only` skips that check entirely — cheaper, and the right setting for
    a quiet assistant in a busy room.
    """
    if respond_mode not in ("mention_or_relevant", "mention_only"):
        typer.echo(
            "MODE must be 'mention_or_relevant' or 'mention_only'", err=True,
        )
        raise typer.Exit(code=1)

    def build(_found: dict[str, Any]) -> dict[str, Any]:
        return {"respond_mode": respond_mode}

    _patch_group_settings(ctx, channel_id, group, build)


@groups_app.command("refresh")
@graceful_errors
def channel_groups_refresh(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    group: str = typer.Argument(..., help="Group id, chat id, or unique title."),
) -> None:
    """Ask the platform who is in a group, now.

    Needs the channel to be running — the member list comes from the platform,
    not from Cremind. Platforms that name nobody report `unsupported` rather
    than failing.
    """
    from app.cli.client._base import Client
    from app.cli.client.channels import (
        refresh_channel_group_roster,
        resolve_channel_group,
    )
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            found = await resolve_channel_group(client, channel_id, group)
            return await refresh_channel_group_roster(
                client, channel_id, str(found["id"]),
            )

    result = _run_group_async(_run())

    if mode.json:
        print_json(result)
        return
    group_row = result.get("group") or {}
    print_kv([
        ("id", str(group_row.get("id") or "")),
        ("title", str(group_row.get("title") or "")),
        ("members", str(group_row.get("member_count") or 0)),
        ("source", str(result.get("source") or "")),
    ])


@groups_app.command("available")
@graceful_errors
def channel_groups_available(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
) -> None:
    """List the groups this channel's account is ALREADY in.

    The way to reach a group nobody added the agent to. A join event only fires
    while Cremind is watching, so groups the account belonged to beforehand are
    never announced — this lists them, and `channels groups add` enables the
    ones you want.

    Platforms that cannot enumerate groups (a Telegram bot, the Zalo bot) say so
    rather than returning an empty list.
    """
    from app.cli.client._base import Client
    from app.cli.client.channels import list_available_channel_groups
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_available_channel_groups(client, channel_id)

    result = _run_group_async(_run())

    if mode.json:
        print_json(result)
        return
    if not result.get("supported"):
        sys.stdout.write(
            "this platform will not list the groups an account is in — add the "
            "account to a group and say something there, and it appears here as "
            "pending.\n"
        )
        return
    groups = result.get("groups") or []
    if not groups:
        sys.stdout.write("this account is not in any groups.\n")
        return
    table = Table(mode, "CHAT_ID", "TITLE", "MEMBERS", "TRACKED")
    for group in groups:
        tracked = group.get("tracked") or {}
        table.add_row(
            str(group.get("platform_chat_id") or ""),
            str(group.get("title") or ""),
            str(group.get("member_count") or ""),
            str(tracked.get("status") or "-"),
        )
    table.render()


@groups_app.command("add")
@graceful_errors
def channel_groups_add(
    ctx: typer.Context,
    channel_id: str = typer.Argument(..., help="Channel id."),
    chat_ids: list[str] = typer.Argument(
        ...,
        help=(
            "Platform chat ids, from `channels groups available`. Telegram and "
            "Zalo ids start with '-', so put `--` before them."
        ),
    ),
    title: str = typer.Option(
        "", "--title", help="Title to store (only meaningful for one chat id).",
    ),
) -> None:
    """Enable one or more groups the account is already in.

    Approved immediately: naming a specific group out of your own list IS the
    approval, so there is no second step. A group already known to Cremind is
    approved rather than duplicated.

    A Telegram or Zalo group id starts with a minus sign, which any CLI reads as
    the start of an option — put `--` before the ids:

        cremind channels groups add <channel_id> -- -1001987654321
    """
    from app.cli.client._base import Client
    from app.cli.client.channels import add_channel_group
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async with Client(cfg) as client:
            for chat_id in chat_ids:
                out.append(await add_channel_group(
                    client, channel_id, chat_id,
                    # One title cannot name several groups; with more than one
                    # pick the platform's own names are the only sensible ones.
                    title=title if len(chat_ids) == 1 else "",
                ))
        return out

    groups = _run_group_async(_run())

    if mode.json:
        print_json({"groups": groups})
        return
    table = Table(mode, "GROUP_ID", "CHAT_ID", "TITLE", "STATUS")
    for group in groups:
        table.add_row(
            str(group.get("id") or ""),
            str(group.get("platform_chat_id") or ""),
            str(group.get("title") or ""),
            str(group.get("status") or ""),
        )
    table.render()
