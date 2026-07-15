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
from app.cli.modes import ChatMode


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
    chat_mode: Optional[ChatMode] = typer.Option(
        None, "--mode", case_sensitive=False,
        help=(
            "Turn mode: plan (research + clarifying questions + approved plan, "
            "then execute), reasoning (default), instant (no extended thinking; "
            "at most one round of tool calls)."
        ),
    ),
    no_reasoning: bool = typer.Option(
        False, "--no-reasoning", help="Deprecated: alias for --mode instant.",
    ),
) -> None:
    """Send a message; streams the agent run to stdout.

    `--json` (root flag) emits JSONL. In plan mode the clarifying-question /
    plan / todo prompts print on stderr (stdout stays assistant text only).
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

    # --no-reasoning is the deprecated alias for --mode instant.
    if no_reasoning:
        if chat_mode is not None and chat_mode != ChatMode.instant:
            raise typer.BadParameter("--no-reasoning conflicts with --mode; pass --mode only")
        sys.stderr.write("warning: --no-reasoning is deprecated; use --mode instant\n")
        chat_mode = ChatMode.instant

    turn_mode = chat_mode.value if chat_mode is not None else None
    renderer = JSONRenderer() if mode.json else RawRenderer(conversation_id=conv_id)

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_stream(
                client,
                conv_id,
                send_text=message,
                mode=turn_mode,
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

    renderer = JSONRenderer() if mode.json else RawRenderer(conversation_id=conv_id)

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


@conv_app.command("memory")
@graceful_errors
def conv_memory(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
) -> None:
    """Show a conversation's running summary + long-term memory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import get_memory
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_memory(client, conv_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    progress = out.get("token_progress") or {}
    sys.stdout.write(f"enabled: {out.get('enabled')}\n")
    sys.stdout.write(f"last_compacted_at: {out.get('last_compacted_at')}\n")
    if isinstance(progress, dict):
        sys.stdout.write(
            f"context: {progress.get('current')} / {progress.get('threshold')} "
            f"(window {progress.get('context_window')})\n"
        )
    sys.stdout.write("\n--- running summary ---\n")
    sys.stdout.write((out.get("summary") or "") + "\n")
    long_term = out.get("long_term") or []
    sys.stdout.write("\n--- long-term memory ---\n")
    if isinstance(long_term, list):
        for item in long_term:
            if isinstance(item, dict):
                sys.stdout.write(f"- {item.get('text') or item.get('content') or item}\n")
            else:
                sys.stdout.write(f"- {item}\n")


@conv_app.command("compact")
@graceful_errors
def conv_compact(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
) -> None:
    """Force a compaction now (folds old turns into the running summary)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import trigger_memory
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> bool:
        async with Client(cfg) as client:
            return await trigger_memory(client, conv_id)

    compacted = asyncio.run(_run())

    if mode.json:
        print_json({"compacted": compacted})
    else:
        sys.stdout.write("compacted\n" if compacted else "no change\n")


@conv_app.command("usage")
@graceful_errors
def conv_usage(
    ctx: typer.Context,
    conv_id: str = typer.Argument(..., help="Conversation id."),
) -> None:
    """Show per-request + cumulative token usage & cost for a conversation."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import get_usage
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json, print_map
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_usage(client, conv_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    totals = out.get("totals") if isinstance(out.get("totals"), dict) else {}
    headline = {
        "conversation_id": out.get("conversation_id"),
        "request_count": out.get("request_count"),
        "cache_hit_rate": out.get("cache_hit_rate"),
        **totals,
    }
    print_map(headline)

    requests = out.get("requests") or []
    requests = [r for r in requests if isinstance(r, dict)]
    if requests:
        sys.stdout.write("\n--- requests ---\n")
        table = Table(mode, "CREATED_AT", "MODEL", "PROVIDER", "TOKENS", "COST_USD")
        for r in requests:
            table.add_row(
                string_field(r, "created_at"),
                string_field(r, "model"),
                string_field(r, "provider"),
                string_field(r, "total_tokens"),
                string_field(r, "cost_usd"),
            )
        table.render()


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
