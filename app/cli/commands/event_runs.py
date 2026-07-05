"""`cremind event-runs ...` — inspect and manage event runs.

Each fired event trigger — a skill event, a file-watcher change, or a
schedule/calendar event — runs in its own isolated, hidden conversation and is
tracked by an ``event_runs`` row carrying a status
(``running``/``pending``/``completed``/``failed``/``cancelled``) and a per-run
token-usage rollup. This group lists that run history, shows one run in detail,
replies to a run that is pending your input, and deletes run history.

Mirrors the Events-page run-history child tables and the run-detail drawer in
the web UI.
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


event_runs_app = typer.Typer(
    name="event-runs",
    help="Inspect and manage event runs (per-trigger execution history).",
    no_args_is_help=True,
)


# Friendly ``--kind`` values → the canonical ``source_kind`` the API filters on.
_KIND_MAP = {
    "skill": "skill_event",
    "skill-event": "skill_event",
    "skill-events": "skill_event",
    "skill_event": "skill_event",
    "file": "file_watcher",
    "file-watcher": "file_watcher",
    "file-watchers": "file_watcher",
    "file_watcher": "file_watcher",
    "watcher": "file_watcher",
    "schedule": "schedule",
    "calendar": "schedule",
}

# Per-status rich styles for the STATUS column (pending is highlighted).
_STATUS_STYLE = {
    "running": "cyan",
    "pending": "bold yellow",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim",
}


def _normalize_kind(kind: Optional[str]) -> Optional[str]:
    """Map a friendly ``--kind`` value to the canonical ``source_kind``.

    Unknown values pass through unchanged so the server can reject them.
    """
    if not kind:
        return None
    return _KIND_MAP.get(kind.strip().lower(), kind.strip())


def _fmt_ts(value: Any) -> str:
    """Epoch **milliseconds** → local ``YYYY-MM-DD HH:MM:SS`` (blank if unset)."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _fmt_usd(value: Any) -> str:
    """Render a cost figure like `cremind usage` does — a plain dollar amount."""
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return ""


def _short(value: Any, width: int = 8) -> str:
    """First ``width`` characters of an id (full ids come from `--json`)."""
    s = "" if value is None else str(value)
    return s[:width]


def _status_cell(mode, status: str) -> str:
    """Colorize a status for the rich table (plain when piped / --no-color)."""
    from app.cli.output.console import is_tty

    if not status:
        return ""
    if is_tty() and not mode.no_color:
        style = _STATUS_STYLE.get(status)
        if style:
            return f"[{style}]{status}[/{style}]"
    return status


@event_runs_app.command("list")
@graceful_errors
def event_runs_list(
    ctx: typer.Context,
    kind: Optional[str] = typer.Option(
        None, "--kind",
        help="Filter by source: skill_event | file_watcher | schedule "
             "(friendly skill / file-watcher / schedule accepted).",
    ),
    subscription: Optional[str] = typer.Option(
        None, "--subscription",
        help="Filter by the originating subscription/event id.",
    ),
    status: Optional[str] = typer.Option(
        None, "--status",
        help="Filter by status: running | pending | completed | failed | cancelled.",
    ),
    limit: int = typer.Option(50, "--limit", help="Max runs to return (newest first)."),
) -> None:
    """List event runs for the active profile (newest first)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.event_runs import list_runs
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_runs(
                client,
                kind=_normalize_kind(kind),
                subscription=subscription,
                status=status,
                limit=limit,
            )

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    runs = [r for r in (out.get("runs") or []) if isinstance(r, dict)]
    if not runs:
        sys.stdout.write("no event runs match.\n")
        return

    table = Table(mode, "FIRED", "STATUS", "LABEL", "TOKENS", "COST", "TURNS", "RUN ID")
    for r in runs:
        usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        table.add_row(
            _fmt_ts(r.get("created_at")),
            _status_cell(mode, string_field(r, "status")),
            string_field(r, "label"),
            string_field(usage, "total_tokens"),
            _fmt_usd(usage.get("total_usd")),
            string_field(r, "turn_count"),
            _short(r.get("id")),
        )
    table.render()

    total = out.get("total")
    if total is not None:
        sys.stdout.write(f"\n{len(runs)} shown / {total} total\n")


@event_runs_app.command("show")
@graceful_errors
def event_runs_show(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Event-run id (from `event-runs list --json`)."),
) -> None:
    """Show one event run in detail (status, usage, pending question, error)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.event_runs import get_run
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_run(client, run_id)

    run = asyncio.run(_run())

    if mode.json:
        print_json(run)
        return

    if not run:
        sys.stdout.write("run not found.\n")
        return

    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    rows: list[tuple[str, str]] = [
        ("id", string_field(run, "id")),
        ("status", string_field(run, "status")),
        ("source_kind", string_field(run, "source_kind")),
        ("subscription_id", string_field(run, "subscription_id")),
        ("label", string_field(run, "label")),
        ("action", string_field(run, "action")),
        ("conversation_id", string_field(run, "conversation_id")),
        ("run_id", string_field(run, "run_id")),
        ("turn_count", string_field(run, "turn_count")),
        ("fired", _fmt_ts(run.get("created_at"))),
        ("updated", _fmt_ts(run.get("updated_at"))),
        ("finished", _fmt_ts(run.get("finished_at"))),
    ]
    pending = run.get("pending_question")
    if pending:
        rows.append(("pending_question", str(pending)))
    error = run.get("error")
    if error:
        rows.append(("error", str(error)))
    print_kv(rows)

    sys.stdout.write("\n--- usage ---\n")
    print_kv([
        ("input_tokens", string_field(usage, "input_tokens")),
        ("cache_read_input_tokens", string_field(usage, "cache_read_input_tokens")),
        ("cache_creation_input_tokens", string_field(usage, "cache_creation_input_tokens")),
        ("output_tokens", string_field(usage, "output_tokens")),
        ("total_tokens", string_field(usage, "total_tokens")),
        ("total_usd", _fmt_usd(usage.get("total_usd"))),
        ("request_count", string_field(usage, "request_count")),
    ])

    conv_id = string_field(run, "conversation_id")
    if conv_id:
        run_pk = string_field(run, "id")
        sys.stdout.write(f"\nTranscript:  cremind conv get {conv_id}\n")
        if string_field(run, "status") == "pending" or pending:
            sys.stdout.write(f'Reply:       cremind event-runs reply {run_pk} "..."\n')


@event_runs_app.command("reply")
@graceful_errors
def event_runs_reply(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Event-run id to reply to."),
    message: str = typer.Argument(..., help="Message to send into the run's conversation."),
) -> None:
    """Reply to a run that is pending your input (resumes its conversation)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.event_runs import get_run
    from app.cli.client.event_runs import reply as _reply
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            run = await get_run(client, run_id)
            conv_id = string_field(run, "conversation_id")
            status = string_field(run, "status")
            if not run:
                return {"error": "not_found"}
            if not conv_id:
                return {"error": "no_conversation", "status": status}
            resp = await _reply(client, conv_id, message)
            return {
                "sent": True,
                "conversation_id": conv_id,
                "status": status,
                "response": resp,
            }

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    err = out.get("error")
    if err == "not_found":
        sys.stdout.write("run not found.\n")
        return
    if err == "no_conversation":
        sys.stdout.write(
            "this run has no conversation yet, so there's nothing to reply to "
            f"(status: {out.get('status') or 'unknown'}).\n"
        )
        return

    status = out.get("status")
    if status and status != "pending":
        sys.stdout.write(
            f"note: run status is '{status}', not 'pending' — sending anyway; "
            "the backend resumes the conversation if it can.\n"
        )
    sys.stdout.write(f"sent to conversation {out.get('conversation_id')}.\n")


@event_runs_app.command("delete")
@graceful_errors
def event_runs_delete(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Event-run id to delete."),
) -> None:
    """Delete a run and its hidden conversation (its usage rollup survives)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.event_runs import delete_run
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_run(client, run_id)

    asyncio.run(_run())
