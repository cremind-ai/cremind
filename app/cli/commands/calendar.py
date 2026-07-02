"""`cremind calendar ...` — manage the Calendar & Schedule feature.

Covers the per-profile feature switch, the calendar occurrence view, manual
schedule-event CRUD, Google Calendar connect/disconnect, and the raw
schedule-event subscriptions (list / pause / resume / cancel). Mirrors the
**Calendar & Schedule** sidebar page and the Events-page "Schedule Events"
section.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


calendar_app = typer.Typer(
    name="calendar",
    help="Manage the Calendar & Schedule feature.",
    no_args_is_help=True,
)
google_app = typer.Typer(
    name="google",
    help="Connect or disconnect Google Calendar.",
    no_args_is_help=True,
)
schedule_app = typer.Typer(
    name="schedule",
    help="Inspect and control raw schedule-event subscriptions.",
    no_args_is_help=True,
)
calendar_app.add_typer(google_app, name="google")
calendar_app.add_typer(schedule_app, name="schedule")


@calendar_app.command("settings")
@graceful_errors
def calendar_settings(ctx: typer.Context) -> None:
    """Show the feature switch + Google connection status."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import get_settings
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_settings(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        print_map(out)


def _set_enabled(ctx: typer.Context, enabled: bool) -> None:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import set_enabled
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await set_enabled(client, enabled)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(f"enabled: {out.get('enabled')}\n")


@calendar_app.command("enable")
@graceful_errors
def calendar_enable(ctx: typer.Context) -> None:
    """Turn the Calendar & Schedule feature on for this profile."""
    _set_enabled(ctx, True)


@calendar_app.command("disable")
@graceful_errors
def calendar_disable(ctx: typer.Context) -> None:
    """Turn the Calendar & Schedule feature off for this profile."""
    _set_enabled(ctx, False)


@calendar_app.command("events")
@graceful_errors
def calendar_events(
    ctx: typer.Context,
    range_from: Optional[str] = typer.Option(None, "--from", help="Window start (YYYY-MM-DD or ISO datetime)."),
    range_to: Optional[str] = typer.Option(None, "--to", help="Window end (YYYY-MM-DD or ISO datetime)."),
) -> None:
    """List calendar occurrences in a window (defaults to ~this month)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import list_events
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_events(client, range_from=range_from, range_to=range_to)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return

    events = out.get("events") or []
    table = Table(mode, "ID", "TITLE", "START", "ALL_DAY", "KIND")
    for e in events:
        if not isinstance(e, dict):
            continue
        table.add_row(
            string_field(e, "id"),
            string_field(e, "title"),
            string_field(e, "dtstart") or string_field(e, "start"),
            bool_field(e, "all_day", False),
            string_field(e, "schedule_kind"),
        )
    table.render()


@calendar_app.command("add")
@graceful_errors
def calendar_add(
    ctx: typer.Context,
    title: str = typer.Option(..., "--title", help="Event title."),
    at: str = typer.Option(..., "--at", help="Start (dtstart): YYYY-MM-DD or ISO datetime."),
    action: Optional[str] = typer.Option(
        None, "--action", help="Instruction to run when it fires (default: the title)."
    ),
    duration_minutes: Optional[int] = typer.Option(None, "--duration", help="Duration in minutes (default 30)."),
    all_day: bool = typer.Option(False, "--all-day", help="Mark as an all-day event."),
    rrule: Optional[str] = typer.Option(None, "--rrule", help="iCalendar RRULE for recurrence (e.g. FREQ=WEEKLY)."),
    schedule_kind: Optional[str] = typer.Option(
        None, "--schedule-kind", help="instant | recurrence (inferred from --rrule if unset)."
    ),
    recurrence_end_type: Optional[str] = typer.Option(
        None, "--recurrence-end-type", help="Recurrence end type (e.g. count | until)."
    ),
    recurrence_end_value: Optional[str] = typer.Option(
        None, "--recurrence-end-value", help="Recurrence end value (count or date)."
    ),
) -> None:
    """Create a manual schedule event (requires the feature enabled)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import create_event
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    body: dict[str, Any] = {"title": title, "dtstart": at, "all_day": all_day}
    if action:
        body["action"] = action
    if duration_minutes is not None:
        body["duration_minutes"] = duration_minutes
    if rrule:
        body["rrule"] = rrule
    if schedule_kind:
        body["schedule_kind"] = schedule_kind
    if recurrence_end_type:
        body["recurrence_end_type"] = recurrence_end_type
    if recurrence_end_value is not None:
        body["recurrence_end_value"] = recurrence_end_value

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await create_event(client, body)

    event = asyncio.run(_run())
    if mode.json:
        print_json(event)
    else:
        print_map(event)


@calendar_app.command("edit")
@graceful_errors
def calendar_edit(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Event id to edit."),
    title: Optional[str] = typer.Option(None, "--title", help="New title."),
    at: Optional[str] = typer.Option(None, "--at", help="New start (dtstart)."),
    action: Optional[str] = typer.Option(None, "--action", help="New action instruction."),
    duration_minutes: Optional[int] = typer.Option(None, "--duration", help="New duration in minutes."),
    all_day: Optional[bool] = typer.Option(None, "--all-day/--no-all-day", help="Toggle all-day."),
    rrule: Optional[str] = typer.Option(None, "--rrule", help="New RRULE."),
    schedule_kind: Optional[str] = typer.Option(None, "--schedule-kind", help="instant | recurrence."),
    recurrence_end_type: Optional[str] = typer.Option(None, "--recurrence-end-type", help="Recurrence end type."),
    recurrence_end_value: Optional[str] = typer.Option(None, "--recurrence-end-value", help="Recurrence end value."),
) -> None:
    """Edit a schedule event (only the flags you pass are changed)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import update_event
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    fields: dict[str, Any] = {}
    if title is not None:
        fields["title"] = title
    if at is not None:
        fields["dtstart"] = at
    if action is not None:
        fields["action"] = action
    if duration_minutes is not None:
        fields["duration_minutes"] = duration_minutes
    if all_day is not None:
        fields["all_day"] = all_day
    if rrule is not None:
        fields["rrule"] = rrule
    if schedule_kind is not None:
        fields["schedule_kind"] = schedule_kind
    if recurrence_end_type is not None:
        fields["recurrence_end_type"] = recurrence_end_type
    if recurrence_end_value is not None:
        fields["recurrence_end_value"] = recurrence_end_value

    if not fields:
        typer.echo("nothing to update — pass at least one field flag", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await update_event(client, event_id, fields)

    event = asyncio.run(_run())
    if mode.json:
        print_json(event)
    else:
        print_map(event)


@calendar_app.command("delete")
@graceful_errors
def calendar_delete(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Event id to delete."),
) -> None:
    """Delete a schedule event."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import delete_event
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_event(client, event_id)

    asyncio.run(_run())


@google_app.command("connect")
@graceful_errors
def google_connect(ctx: typer.Context) -> None:
    """Print the Google Calendar authorize URL to open in a browser."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import google_connect as _connect
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await _connect(client)

    url = asyncio.run(_run())
    if mode.json:
        print_json({"authorize_url": url})
    else:
        sys.stdout.write(f"{url}\n")


@google_app.command("disconnect")
@graceful_errors
def google_disconnect(ctx: typer.Context) -> None:
    """Disconnect Google Calendar for this profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import google_disconnect as _disconnect
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await _disconnect(client)

    asyncio.run(_run())


@schedule_app.command("list")
@graceful_errors
def schedule_list(ctx: typer.Context) -> None:
    """List the raw schedule-event subscriptions for this profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import list_subscriptions
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_subscriptions(client)

    subs = asyncio.run(_run())
    if mode.json:
        print_json(subs)
        return
    table = Table(mode, "ID", "TITLE", "KIND", "START", "STATUS", "CONV_TITLE")
    for s in subs:
        table.add_row(
            string_field(s, "id"),
            string_field(s, "title"),
            string_field(s, "schedule_kind"),
            string_field(s, "dtstart"),
            string_field(s, "status"),
            string_field(s, "conversation_title"),
        )
    table.render()


@schedule_app.command("status")
@graceful_errors
def schedule_status(
    ctx: typer.Context,
    event_id: str = typer.Argument(..., help="Schedule event id."),
    status: str = typer.Argument(..., help="active | paused | cancelled.", metavar="STATUS"),
) -> None:
    """Pause, resume (active), or cancel a schedule event."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.calendar import set_status
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await set_status(client, event_id, status)

    event = asyncio.run(_run())
    if mode.json:
        print_json(event)
    else:
        print_map(event)
