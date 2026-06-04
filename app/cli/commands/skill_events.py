"""`cremind skill-events ...` — manage skill event subscriptions.

Mirrors `cli/cmd/skillevents.go`. Phase 2 covers the non-streaming
subcommands; `stream` and `notifications` (SSE) are added in Phase 3.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any

import typer

from app.cli.commands._helpers import graceful_errors


skill_events_app = typer.Typer(
    name="skill-events",
    help="Manage skill event subscriptions and tail notifications.",
    no_args_is_help=True,
)


@skill_events_app.command("list")
@graceful_errors
def skill_events_list(ctx: typer.Context) -> None:
    """List skill event subscriptions for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import list_skill_event_subscriptions
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_skill_event_subscriptions(client)

    subs = asyncio.run(_run())

    if mode.json:
        print_json(subs)
        return

    table = Table(mode, "ID", "SKILL", "EVENT_TYPE", "CONVERSATION", "CONV_TITLE")
    for s in subs:
        table.add_row(
            string_field(s, "id"),
            string_field(s, "skill_name"),
            string_field(s, "event_type"),
            string_field(s, "conversation_id"),
            string_field(s, "conversation_title"),
        )
    table.render()


@skill_events_app.command("delete")
@graceful_errors
def skill_events_delete(
    ctx: typer.Context,
    sub_id: str = typer.Argument(..., help="Subscription id."),
) -> None:
    """Delete a skill event subscription."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import delete_skill_event_subscription
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_skill_event_subscription(client, sub_id)

    asyncio.run(_run())


@skill_events_app.command("simulate")
@graceful_errors
def skill_events_simulate(
    ctx: typer.Context,
    sub_id: str = typer.Argument(..., help="Subscription id."),
    filename: str = typer.Option(
        "", "--filename",
        help="Optional filename (defaults to a unique simulate-*.md).",
    ),
) -> None:
    """Drop a markdown file into the watched events folder (dev tool)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import simulate_skill_event
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    body = sys.stdin.read()

    async def _run() -> None:
        async with Client(cfg) as client:
            await simulate_skill_event(client, sub_id, body, filename)

    asyncio.run(_run())


@skill_events_app.command("events")
@graceful_errors
def skill_events_events(
    ctx: typer.Context,
    skill: str = typer.Argument(..., help="Skill name."),
) -> None:
    """List the events declared by a skill (from its SKILL.md)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import list_skill_events
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_skill_events(client, skill)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")


@skill_events_app.command("listener-status")
@graceful_errors
def skill_events_listener_status(
    ctx: typer.Context,
    skill: str = typer.Argument(..., help="Skill name."),
) -> None:
    """Show the listener daemon's heartbeat-derived status for a skill."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import get_listener_status
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_listener_status(client, skill)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    print_kv([
        ("skill_name", string_field(out, "skill_name")),
        ("running", bool_field(out, "running", False)),
        ("last_heartbeat", str(out.get("last_heartbeat", ""))),
        ("autostart_id", str(out.get("autostart_id", ""))),
        ("command", string_field(out, "command")),
    ])


@skill_events_app.command("stream")
@graceful_errors
def skill_events_stream(ctx: typer.Context) -> None:
    """Stream the skill-events admin snapshot (SSE)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import skill_events_admin_stream_path
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_admin_stream(client, skill_events_admin_stream_path(), mode)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


@skill_events_app.command("notifications")
@graceful_errors
def skill_events_notifications(
    ctx: typer.Context,
    since: int = typer.Option(0, "--since", help="Resume cursor (millis since epoch)."),
) -> None:
    """Tail per-profile skill-event notifications (SSE)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import skill_event_notifications_stream_path
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_admin_stream(
                client,
                skill_event_notifications_stream_path(since),
                mode,
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


@skill_events_app.command("listener-start")
@graceful_errors
def skill_events_listener_start(
    ctx: typer.Context,
    skill: str = typer.Argument(..., help="Skill name."),
) -> None:
    """Start (or resume) a skill's listener daemon as an autostart process."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skill_events import start_listener
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await start_listener(client, skill)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    print_kv([
        ("process_id", string_field(out, "process_id")),
        ("autostart_id", str(out.get("autostart_id", ""))),
    ])
