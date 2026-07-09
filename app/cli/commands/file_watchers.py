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


@file_watchers_app.command("list")
@graceful_errors
def file_watchers_list(ctx: typer.Context) -> None:
    """List file watcher subscriptions for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import list_file_watchers
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

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
        "ID", "NAME", "PATH", "TRIGGERS", "TARGET", "EXTENSIONS", "ARMED", "CONV_TITLE",
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
) -> None:
    """Register a new file watcher subscription."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import create_file_watcher
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, string_field

    if not action:
        typer.echo("--action is required", err=True)
        raise typer.Exit(code=1)

    body: dict[str, Any] = {"action": action}
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
    print_kv([
        ("id", string_field(out, "id")),
        ("name", string_field(out, "name")),
        ("root_path", string_field(out, "root_path")),
        ("event_types", string_field(out, "event_types")),
        ("target_kind", string_field(out, "target_kind")),
        ("extensions", string_field(out, "extensions")),
        ("recursive", bool_field(out, "recursive", True)),
        ("armed", bool_field(out, "armed", False)),
        ("conversation_id", string_field(out, "conversation_id")),
    ])


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
) -> None:
    """Edit a file watcher (only the flags you pass are changed)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.file_watchers import update_file_watcher
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, string_field

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
    print_kv([
        ("id", string_field(out, "id")),
        ("name", string_field(out, "name")),
        ("root_path", string_field(out, "root_path")),
        ("event_types", string_field(out, "event_types")),
        ("target_kind", string_field(out, "target_kind")),
        ("extensions", string_field(out, "extensions")),
        ("recursive", bool_field(out, "recursive", True)),
        ("armed", bool_field(out, "armed", False)),
        ("conversation_id", string_field(out, "conversation_id")),
    ])


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
