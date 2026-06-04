"""Interactive chat TUI built on prompt_toolkit.

Port of `cli/internal/tui/chat.go`. Maintains four panes vertically:

    +-------------------------------------------+
    | header   : title + conv id                |
    +-------------------------------------------+
    | transcript (auto-scroll to bottom)        |
    | ...                                       |
    +-------------------------------------------+
    | status   : run state + token usage        |
    +-------------------------------------------+
    | > input row                               |
    +-------------------------------------------+

The streaming pipeline runs as a background asyncio task that reads from
`Client.stream(conversation_stream_path(id))`, formats each event via
`renderer.format_event`, and appends to the transcript buffer.

The file-tree side panel from the Go TUI is intentionally deferred — the
Go behavior is replicated in two follow-up commits (file picker + watch
SSE) without changing the layout shell here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.widgets import TextArea

from app.cli.client._base import Client
from app.cli.client.conversations import (
    cancel_task,
    conversation_stream_path,
    send_message,
)
from app.cli.config import Config
from app.cli.tui.renderer import (
    RenderedLine,
    Theme,
    default_theme,
    extract_token_usage,
    format_event,
    monochrome_theme,
)


@dataclass
class _ChatState:
    title: str
    transcript: list[RenderedLine] = field(default_factory=list)
    status: str = "connecting..."
    tokens: str = ""
    run_id: Optional[str] = None


async def run_chat(
    cfg: Config,
    conv_id: str,
    conv_title: str,
    *,
    initial_message: str = "",
) -> None:
    """Open the chat TUI for `conv_id` and block until the user quits."""
    theme = monochrome_theme() if cfg.no_color else default_theme()
    state = _ChatState(title=conv_title or conv_id)

    if initial_message:
        # Echo the initial message into the transcript so the user sees their
        # outgoing turn before the agent starts responding.
        state.transcript.append(
            RenderedLine(
                "user",
                theme.style(theme.user_msg, "you: ") + initial_message,
            )
        )

    pending: asyncio.Queue[str] = asyncio.Queue()
    if initial_message:
        pending.put_nowait(initial_message)

    # ── widgets ──────────────────────────────────────────────────────────

    transcript_area = TextArea(
        text="",
        read_only=True,
        scrollbar=False,
        wrap_lines=True,
        focusable=False,
    )

    input_buffer = Buffer(multiline=False)

    def _set_transcript_text() -> None:
        """Render `state.transcript` into the read-only transcript buffer
        and place the cursor at the very end so prompt_toolkit auto-scrolls
        to the latest content.
        """
        text = "\n".join(line.body for line in state.transcript)
        transcript_area.buffer.set_document(
            Document(text, cursor_position=len(text)),
            bypass_readonly=True,
        )

    def _get_header() -> ANSI:
        title = state.title
        return ANSI(
            f"\x1b[1;38;5;230;48;5;63m {title} \x1b[0m"
            f"  \x1b[38;5;240m{conv_id}\x1b[0m"
        )

    def _get_status() -> ANSI:
        parts = [state.status]
        if state.tokens:
            parts.append(state.tokens)
        body = "  ".join(parts)
        return ANSI(f"\x1b[48;5;236;38;5;250m {body} \x1b[0m")

    def _get_prompt() -> ANSI:
        return ANSI(theme.style(theme.user_msg, "> "))

    # ── key bindings ─────────────────────────────────────────────────────

    kb = KeyBindings()

    @kb.add("enter")
    def _on_enter(event) -> None:
        text = input_buffer.text.strip()
        if not text:
            return
        input_buffer.reset()
        state.transcript.append(
            RenderedLine("user", theme.style(theme.user_msg, "you: ") + text)
        )
        _set_transcript_text()
        try:
            pending.put_nowait(text)
        except asyncio.QueueFull:
            state.status = "send queue full"

    @kb.add("c-c")
    def _on_ctrl_c(event) -> None:
        run_id = state.run_id
        if run_id:
            asyncio.get_running_loop().create_task(_cancel_run(cfg, run_id))
            state.status = "cancelling..."
            event.app.invalidate()
        else:
            event.app.exit()

    @kb.add("c-d")
    def _on_ctrl_d(event) -> None:
        event.app.exit()

    @kb.add("pageup")
    def _on_pgup(event) -> None:
        # Forward to the transcript so the user can scroll back through history.
        transcript_area.buffer.cursor_up(count=10)

    @kb.add("pagedown")
    def _on_pgdn(event) -> None:
        transcript_area.buffer.cursor_down(count=10)

    # ── layout ───────────────────────────────────────────────────────────

    input_row = VSplit([
        Window(
            content=FormattedTextControl(_get_prompt),
            width=2,
            dont_extend_width=True,
        ),
        Window(
            content=BufferControl(buffer=input_buffer),
            wrap_lines=False,
        ),
    ])

    layout = Layout(
        container=HSplit([
            Window(content=FormattedTextControl(_get_header), height=1),
            transcript_area,
            Window(content=FormattedTextControl(_get_status), height=1),
            Window(content=FormattedTextControl(lambda: ""), height=1),  # spacer
            input_row,
        ]),
    )

    application: Application = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        mouse_support=False,
    )

    # ── async tasks ──────────────────────────────────────────────────────

    async def _consume_stream(client: Client) -> None:
        try:
            async for event in client.stream(conversation_stream_path(conv_id)):
                if event.type == "token_usage":
                    tok = extract_token_usage(event)
                    if tok:
                        state.tokens = tok
                        application.invalidate()
                    continue

                if event.type == "ready":
                    state.status = "ready"
                elif event.type == "complete":
                    state.status = "idle"
                    state.run_id = None
                elif event.type == "error":
                    state.status = "error"
                    state.run_id = None

                line = format_event(event, theme)
                if line is not None:
                    if (
                        line.kind == "text"
                        and state.transcript
                        and state.transcript[-1].kind == "text"
                    ):
                        prev = state.transcript[-1]
                        state.transcript[-1] = RenderedLine(
                            "text",
                            prev.body + line.body,
                        )
                    else:
                        state.transcript.append(line)
                _set_transcript_text()
                application.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state.status = f"stream error: {e}"
            application.invalidate()

    async def _send_pending(client: Client) -> None:
        try:
            while True:
                text = await pending.get()
                try:
                    resp = await send_message(client, conv_id, text, reasoning=True)
                    state.run_id = resp.run_id
                    state.status = "running"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    state.status = f"send failed: {e}"
                application.invalidate()
        except asyncio.CancelledError:
            raise

    # ── run ──────────────────────────────────────────────────────────────

    _set_transcript_text()

    async with Client(cfg) as client:
        consumer = asyncio.create_task(_consume_stream(client), name="chat.consumer")
        sender = asyncio.create_task(_send_pending(client), name="chat.sender")
        try:
            await application.run_async()
        finally:
            consumer.cancel()
            sender.cancel()
            for task in (consumer, sender):
                try:
                    await task
                except (asyncio.CancelledError, BaseException):
                    pass


async def _cancel_run(cfg: Config, run_id: str) -> None:
    """Best-effort cancel of an in-flight run; failures are silent."""
    try:
        async with Client(cfg) as client:
            await cancel_task(client, run_id)
    except Exception:
        pass
