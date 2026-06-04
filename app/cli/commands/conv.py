"""`cremind conv ...` — manage conversations and stream agent runs.

Mirrors `cli/cmd/conv.go`. The interactive TUI used by `conv send`/`attach`
and `conv get --detail` lands in Phase 4 — until then those commands fall
back to plain stdout streaming (`RawRenderer`), which is what `--raw` does
in the Go CLI today.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


conv_app = typer.Typer(
    name="conv",
    help="Manage conversations and stream agent runs.",
    no_args_is_help=True,
)


@conv_app.command("list")
@graceful_errors
def conv_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", help="Page size."),
    offset: int = typer.Option(0, "--offset", help="Offset for pagination."),
    channel: Optional[str] = typer.Option(
        None, "--channel",
        help="Filter by channel_type (e.g. main, telegram).",
    ),
) -> None:
    """List conversations for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import list_conversations
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_conversations(client, limit, offset, channel or "")

    convs = asyncio.run(_run())

    if mode.json:
        print_json(convs)
        return
    table = Table(mode, "ID", "TITLE", "CHANNEL", "CREATED_AT", "TASK_ID")
    for c in convs:
        table.add_row(
            string_field(c, "id"),
            string_field(c, "title"),
            string_field(c, "channel_id"),
            string_field(c, "created_at"),
            string_field(c, "task_id"),
        )
    table.render()


@conv_app.command("new")
@graceful_errors
def conv_new(
    ctx: typer.Context,
    title: str = typer.Option("", "--title", "-t", help="Conversation title."),
) -> None:
    """Create a new conversation; prints the conversation id."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import create_conversation
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.console import is_tty
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await create_conversation(client, title)

    conv = asyncio.run(_run())

    if mode.json:
        print_json(conv)
        return
    conv_id = string_field(conv, "id")
    if is_tty():
        print_kv([
            ("id", conv_id),
            ("title", string_field(conv, "title")),
            ("created_at", string_field(conv, "created_at")),
        ])
    else:
        sys.stdout.write(f"{conv_id}\n")


@conv_app.command("get")
@graceful_errors
def conv_get(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
    detail: bool = typer.Option(
        False, "--detail",
        help="Open a TUI replay (Phase 4 — falls back to plain output for now).",
    ),
) -> None:
    """Fetch a conversation with its full message history."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import get_conversation
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_conversation(client, conv_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    conv = out.get("conversation") if isinstance(out.get("conversation"), dict) else {}
    messages = out.get("messages") if isinstance(out.get("messages"), list) else []

    if detail:
        sys.stderr.write(
            "(--detail TUI replay arrives in Phase 4 of the migration; "
            "falling back to plain output)\n"
        )

    print_kv([
        ("id", string_field(conv, "id")),
        ("title", string_field(conv, "title")),
        ("task_id", string_field(conv, "task_id")),
        ("created_at", string_field(conv, "created_at")),
    ])
    sys.stdout.write("\n--- messages ---\n")
    for m in messages:
        if not isinstance(m, dict):
            continue
        sys.stdout.write(f"[{string_field(m, 'role')}] {string_field(m, 'content')}\n")


@conv_app.command("history")
@graceful_errors
def conv_history(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
    limit: int = typer.Option(100, "--limit", help="Page size."),
    offset: int = typer.Option(0, "--offset", help="Offset for pagination."),
) -> None:
    """Show paginated message history for a conversation."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import get_messages
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await get_messages(client, conv_id, limit, offset)

    msgs = asyncio.run(_run())

    if mode.json:
        print_json(msgs)
        return
    for m in msgs:
        sys.stdout.write(f"[{string_field(m, 'role')}] {string_field(m, 'content')}\n")


@conv_app.command("send")
@graceful_errors
def conv_send(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
    message: str = typer.Argument(..., help="User message."),
    raw: bool = typer.Option(False, "--raw", help="Plain text output (currently the default)."),
    no_reasoning: bool = typer.Option(False, "--no-reasoning", help="Disable reasoning mode."),
) -> None:
    """Send a message; streams the agent run to stdout.

    Phase 4 will add an interactive TUI; for now both default and `--raw`
    behave the same. `--json` (root flag) emits JSONL.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import JSONRenderer, RawRenderer, run_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()
    _ = raw  # accepted for forward-compatibility; raw is the only mode in Phase 3

    renderer = JSONRenderer() if mode.json else RawRenderer()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_stream(
                client,
                conv_id,
                send_text=message,
                reasoning=not no_reasoning,
                renderer=renderer,
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


@conv_app.command("attach")
@graceful_errors
def conv_attach(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
    raw: bool = typer.Option(False, "--raw", help="Plain text output (currently the default)."),
) -> None:
    """Subscribe to a conversation's live run without sending a message.

    Phase 4 will add an interactive TUI; for now both default and `--raw`
    behave the same. `--json` (root flag) emits JSONL.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import JSONRenderer, RawRenderer, run_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()
    _ = raw

    renderer = JSONRenderer() if mode.json else RawRenderer()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_stream(client, conv_id, renderer=renderer)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


@conv_app.command("rename")
@graceful_errors
def conv_rename(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
    title: str = typer.Argument(..., help="New title."),
) -> None:
    """Set the title of a conversation."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import update_conversation
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await update_conversation(client, conv_id, {"title": title})

    asyncio.run(_run())


@conv_app.command("set-id")
@graceful_errors
def conv_set_id(
    ctx: typer.Context,
    old_id: str = typer.Argument(..., help="Current conversation id."),
    new_id: str = typer.Argument(..., help="New conversation id."),
) -> None:
    """Change a conversation's id (also resets title to the new id)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import update_conversation
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await update_conversation(client, old_id, {"id": new_id})

    asyncio.run(_run())


@conv_app.command("cancel")
@graceful_errors
def conv_cancel(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="Run id (or task id)."),
) -> None:
    """Cancel an in-flight agent run."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import cancel_task
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> bool:
        async with Client(cfg) as client:
            return await cancel_task(client, run_id)

    cancelled = asyncio.run(_run())

    if mode.json:
        print_json({"cancelled": cancelled})
    else:
        sys.stdout.write("cancelled\n" if cancelled else "no active run for that id\n")


@conv_app.command("delete")
@graceful_errors
def conv_delete(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
) -> None:
    """Delete a single conversation."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import delete_conversation
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_conversation(client, conv_id)

    asyncio.run(_run())


@conv_app.command("delete-all")
@graceful_errors
def conv_delete_all(ctx: typer.Context) -> None:
    """Delete every conversation for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import delete_all_conversations
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> int:
        async with Client(cfg) as client:
            return await delete_all_conversations(client)

    n = asyncio.run(_run())

    if mode.json:
        print_json({"deleted_count": n})
    else:
        sys.stdout.write(f"deleted {n} conversation(s)\n")
