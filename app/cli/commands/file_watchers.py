"""`cremind file-watchers ...` — manage filesystem watch subscriptions.

Mirrors `cli/cmd/filewatchers.go`. Phase 2 covers list/delete/register; the
`stream` SSE subcommand is added in Phase 3.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


file_watchers_app = typer.Typer(
    name="file-watchers",
    help="Manage filesystem watch subscriptions.",
    no_args_is_help=True,
)


def _watcher_kv(out: dict[str, Any], string_field, bool_field, epoch_seconds_field):
    """The key/value rows printed after a register or edit."""
    rows = [
        ("id", string_field(out, "id")),
        ("name", string_field(out, "name")),
        ("root_path", string_field(out, "root_path")),
        ("event_types", string_field(out, "event_types")),
        ("target_kind", string_field(out, "target_kind")),
        ("extensions", string_field(out, "extensions")),
        ("recursive", bool_field(out, "recursive", True)),
        ("armed", bool_field(out, "armed", False)),
    ]
    if out.get("task"):
        rows.append(("task_status", string_field(out, "task_status")))
        rows.append(("timeout_at", epoch_seconds_field(out.get("timeout_at"))))
    rows.append(("conversation_id", string_field(out, "conversation_id")))
    return rows


@file_watchers_app.command("list")
@graceful_errors
def file_watchers_list(ctx: typer.Context) -> None:
    """List file watcher subscriptions for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import list_file_watchers
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, epoch_seconds_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_file_watchers(client)

    subs = asyncio.run(_run())

    if mode.json:
        print_json(subs)
        return

    table = Table(
        mode,
        "ID", "NAME", "PATH", "TRIGGERS", "TARGET", "EXTENSIONS", "ARMED", "PAUSED",
        "TASK", "TIMEOUT", "CONV_TITLE",
    )
    for s in subs:
        table.add_row(
            string_field(s, "id"),
            string_field(s, "name"),
            string_field(s, "root_path"),
            string_field(s, "event_types"),
            string_field(s, "target_kind"),
            string_field(s, "extensions"),
            bool_field(s, "armed", False),
            bool_field(s, "paused", False),
            # TASK shows the one-shot lifecycle. Blank for a standing watcher,
            # which keeps firing and reports every firing back to its
            # conversation.
            (s.get("task_status") or "active") if s.get("task") else "",
            epoch_seconds_field(s.get("timeout_at")),
            string_field(s, "conversation_title"),
        )
    table.render()


@file_watchers_app.command("delete")
@graceful_errors
def file_watchers_delete(
    ctx: typer.Context,
    watcher_id: str = typer.Argument(..., help="Watcher id."),
) -> None:
    """Delete a file watcher subscription."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import delete_file_watcher
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_file_watcher(client, watcher_id)

    asyncio.run(_run())


def _set_file_watcher_paused(ctx: typer.Context, watcher_id: str, paused: bool) -> None:
    """Shared body for the pause/resume twins — PATCH the ``paused`` flag."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import update_file_watcher
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await update_file_watcher(client, watcher_id, {"paused": paused})

    watcher = asyncio.run(_run())
    if mode.json:
        print_json(watcher)
    else:
        print_map(watcher)


@file_watchers_app.command("pause")
@graceful_errors
def file_watchers_pause(
    ctx: typer.Context,
    watcher_id: str = typer.Argument(..., help="Watcher id."),
) -> None:
    """Pause a file watcher — retained but stops firing runs."""
    _set_file_watcher_paused(ctx, watcher_id, True)


@file_watchers_app.command("resume")
@graceful_errors
def file_watchers_resume(
    ctx: typer.Context,
    watcher_id: str = typer.Argument(..., help="Watcher id."),
) -> None:
    """Resume a paused file watcher."""
    _set_file_watcher_paused(ctx, watcher_id, False)


@file_watchers_app.command("register")
@graceful_errors
def file_watchers_register(
    ctx: typer.Context,
    path: Optional[str] = typer.Option(
        None, "--path",
        help="Directory to watch (relative paths join user working dir).",
    ),
    name: Optional[str] = typer.Option(
        None, "--name",
        help="Optional display name (auto-generated if blank).",
    ),
    triggers: Optional[str] = typer.Option(
        None, "--triggers",
        help="CSV subset of created,modified,deleted,moved (default: all).",
    ),
    target_kind: Optional[str] = typer.Option(
        None, "--target",
        help="file | folder | any (default any).",
    ),
    extensions: Optional[str] = typer.Option(
        None, "--ext",
        help="CSV extensions e.g. .py,.md (file events only; empty = all).",
    ),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive",
        help="Watch subdirectories recursively.",
    ),
    action: Optional[str] = typer.Option(
        None, "--action",
        help="Natural-language instruction the assistant runs on each event.",
    ),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation",
        help="Existing conversation id to bind to (a new one is created if blank).",
    ),
    task: bool = typer.Option(
        False, "--task",
        help=(
            "Register a ONE-SHOT task: fires on the FIRST matching event, "
            "reports its result back into the bound conversation, then removes "
            "itself. Without it the watcher is standing — it keeps watching and "
            "reports each firing back to that same conversation."
        ),
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout",
        help=(
            "Requires --task (a standing watcher has no deadline): minutes to "
            "wait before giving up and reporting that the event never fired "
            "(default 7 days)."
        ),
    ),
) -> None:
    """Register a new file watcher subscription."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import create_file_watcher
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, epoch_seconds_field, string_field

    if not action:
        typer.echo("--action is required", err=True)
        raise typer.Exit(code=1)
    if timeout is not None and not task:
        typer.echo("--timeout requires --task", err=True)
        raise typer.Exit(code=1)

    body: dict[str, Any] = {"action": action}
    if task:
        body["task"] = True
        if timeout is not None:
            body["timeout_minutes"] = timeout
    if path:
        body["path"] = path
    if name:
        body["name"] = name
    if triggers:
        body["triggers"] = _split_csv(triggers)
    if target_kind:
        body["target_kind"] = target_kind
    if extensions:
        body["extensions"] = _split_csv(extensions)
    body["recursive"] = recursive
    if conversation_id:
        body["conversation_id"] = conversation_id

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await create_file_watcher(client, body)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    print_kv(_watcher_kv(out, string_field, bool_field, epoch_seconds_field))


@file_watchers_app.command("edit")
@graceful_errors
def file_watchers_edit(
    ctx: typer.Context,
    watcher_id: str = typer.Argument(..., help="Watcher id to edit."),
    path: Optional[str] = typer.Option(
        None, "--path",
        help="New directory to watch (relative paths join user working dir).",
    ),
    name: Optional[str] = typer.Option(None, "--name", help="New display name."),
    triggers: Optional[str] = typer.Option(
        None, "--triggers",
        help="CSV subset of created,modified,deleted,moved.",
    ),
    target_kind: Optional[str] = typer.Option(
        None, "--target", help="file | folder | any.",
    ),
    extensions: Optional[str] = typer.Option(
        None, "--ext",
        help="CSV extensions e.g. .py,.md (pass empty to match all).",
    ),
    recursive: Optional[bool] = typer.Option(
        None, "--recursive/--no-recursive", help="Toggle recursive watching.",
    ),
    action: Optional[str] = typer.Option(
        None, "--action",
        help="New natural-language instruction the assistant runs on each event.",
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout",
        help=(
            "ONE-SHOT tasks only: minutes from now before the task gives up "
            "and reports back that its event never fired."
        ),
    ),
    no_timeout: bool = typer.Option(
        False, "--no-timeout",
        help=(
            "ONE-SHOT tasks only: clear the deadline (wait indefinitely for "
            "the single firing)."
        ),
    ),
) -> None:
    """Edit a file watcher (only the flags you pass are changed)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import update_file_watcher
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, epoch_seconds_field, string_field

    if timeout is not None and no_timeout:
        typer.echo("--timeout and --no-timeout are mutually exclusive", err=True)
        raise typer.Exit(code=1)

    fields: dict[str, Any] = {}
    if path is not None:
        fields["path"] = path
    if name is not None:
        fields["name"] = name
    if triggers is not None:
        fields["triggers"] = _split_csv(triggers)
    if target_kind is not None:
        fields["target_kind"] = target_kind
    if extensions is not None:
        fields["extensions"] = _split_csv(extensions)
    if recursive is not None:
        fields["recursive"] = recursive
    if action is not None:
        fields["action"] = action
    if timeout is not None:
        fields["timeout_minutes"] = timeout
    elif no_timeout:
        fields["timeout_minutes"] = None

    if not fields:
        typer.echo("nothing to update — pass at least one field flag", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await update_file_watcher(client, watcher_id, fields)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    print_kv(_watcher_kv(out, string_field, bool_field, epoch_seconds_field))


@file_watchers_app.command("stream")
@graceful_errors
def file_watchers_stream(ctx: typer.Context) -> None:
    """Stream the file-watchers admin snapshot (SSE)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import file_watchers_admin_stream_path
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_admin_stream(client, file_watchers_admin_stream_path(), mode)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


def _split_csv(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]
