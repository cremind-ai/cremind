"""`cremind chat` — interactive chat REPL with streamed thinking.

Mirrors `cli/cmd/chat.go`. With no arg, creates a new conversation. With a
conversation id, resumes that conversation.

The TUI itself lives in `app.cli.tui.chat`; this module just resolves the
conversation id/title, then hands off.
"""

from __future__ import annotations

from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors
from app.cli.modes import ChatMode


@graceful_errors
def chat(
    ctx: typer.Context,
    conversation_id: Optional[str] = typer.Argument(
        None,
        help="Existing conversation id; omit to create a new one.",
    ),
    title: Optional[str] = typer.Option(
        None, "--title", "-t",
        help="Title to use when creating a new conversation.",
    ),
    chat_mode: Optional[ChatMode] = typer.Option(
        None, "--mode", case_sensitive=False,
        help="Turn mode for this session: plan, reasoning (default), or instant.",
    ),
) -> None:
    """Open an interactive chat REPL with streamed thinking.

    Keys:
      Enter       send message
      Ctrl+C      cancel current run (or quit if idle)
      Ctrl+D      quit
      PgUp/PgDn   scroll history
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.conversations import (
        create_conversation,
        get_conversation,
    )
    from app.cli.config import Config
    from app.cli.output.formatting import string_field
    from app.cli.tui.chat import run_chat

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _resolve_conversation() -> tuple[str, str]:
        async with Client(cfg) as client:
            if conversation_id:
                out = await get_conversation(client, conversation_id)
                conv = out.get("conversation") if isinstance(out.get("conversation"), dict) else {}
                return conversation_id, string_field(conv, "title")
            conv = await create_conversation(client, title or "")
            return string_field(conv, "id"), string_field(conv, "title")

    async def _main() -> None:
        conv_id, conv_title = await _resolve_conversation()
        await run_chat(
            cfg, conv_id, conv_title,
            mode=chat_mode.value if chat_mode is not None else None,
        )

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
