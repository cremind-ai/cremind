"""Streaming pipeline for SSE-driven commands.

Mirrors `cli/internal/stream/` (Go). Two patterns live here:

* **Conversation stream pipeline** (`run_stream`) — subscribe-first, then
  optionally POST a message after the `ready` event so we can't miss early
  agent events. Used by `cremind conv send` and `cremind conv attach`.
* **Admin stream loop** (`run_admin_stream`) — generic line-mode renderer
  for `proc stream`, `skill-events stream`/`notifications`, and
  `file-watchers stream`.

Renderers consume `Event` instances yielded by `Client.stream(...)`. A
renderer's `render` returns False to stop the loop.
"""

from __future__ import annotations

import sys
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from app.cli.client._base import Client
from app.cli.client._sse import Event
from app.cli.client.conversations import (
    conversation_stream_path,
    send_message as _send_message,
)
from app.cli.output.console import OutputMode
from app.cli.plan_render import plan_hint_lines, questions_lines, todos_lines


# ── renderer protocol ─────────────────────────────────────────────────────


@runtime_checkable
class Renderer(Protocol):
    """Consumer of SSE events. `render` returns False to end the loop."""

    def render(self, event: Event) -> bool: ...
    def stop(self, error: Optional[BaseException]) -> None: ...


class RawRenderer:
    """Print only the assistant `text` events to stdout. Errors + Plan-mode
    prompts are surfaced on stderr (stdout stays assistant-text-only and
    pipe-safe); complete/error end the loop with a trailing newline.

    Mirrors `cli/internal/stream/renderers.go`'s `RawRenderer`.
    """

    def __init__(self, conversation_id: str = "") -> None:
        self.conversation_id = conversation_id

    def render(self, event: Event) -> bool:
        if event.type == "text":
            data = event.data.get("data") if isinstance(event.data, dict) else None
            if isinstance(data, dict):
                text = data.get("text") or data.get("token") or ""
                if isinstance(text, str) and text:
                    sys.stdout.write(text)
                    sys.stdout.flush()
            return True
        if event.type in ("ask_user_question", "plan_ready", "todos"):
            payload = event.data.get("data") if isinstance(event.data, dict) else None
            payload = payload if isinstance(payload, dict) else {}
            if event.type == "ask_user_question":
                lines = questions_lines(payload)
            elif event.type == "plan_ready":
                lines = plan_hint_lines(payload, self.conversation_id)
            else:
                lines = todos_lines(payload)
            for ln in lines:
                sys.stderr.write(ln + "\n")
            sys.stderr.flush()
            return True
        if event.type == "error":
            data = event.data.get("data") if isinstance(event.data, dict) else None
            d = data if isinstance(data, dict) else {}
            msg = str(d.get("message") or d.get("error") or "agent error")
            if d.get("setup_required"):
                sys.stderr.write(f"⚠ Setup required: {msg}\n")
                label = str(d.get("settings_label") or "the Settings page")
                hint = f"  -> Fix it from {label}"
                if d.get("settings_path"):
                    hint += f" ({d['settings_path']})"
                sys.stderr.write(hint + "\n")
            else:
                sys.stderr.write(f"x {msg}\n")
            sys.stdout.write("\n")
            return False
        if event.type == "complete":
            sys.stdout.write("\n")
            return False
        return True

    def stop(self, error: Optional[BaseException]) -> None:
        if error is not None:
            sys.stderr.write(f"{error}\n")


class JSONRenderer:
    """Dump each SSE event verbatim as a JSONL line. Stops on
    `complete` or `error`. Mirrors `JSONRenderer` in the Go renderer.
    """

    def render(self, event: Event) -> bool:
        sys.stdout.write(event.raw)
        if not event.raw.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return event.type not in ("complete", "error")

    def stop(self, error: Optional[BaseException]) -> None:
        if error is not None:
            sys.stderr.write(f"{error}\n")


# ── conversation streaming pipeline ──────────────────────────────────────


async def run_stream(
    client: Client,
    conversation_id: str,
    *,
    send_text: str = "",
    reasoning: bool = True,
    mode: Optional[str] = None,
    renderer: Renderer,
    on_run_id: Optional[Callable[[str], Awaitable[None] | None]] = None,
) -> None:
    """Open the conversation SSE stream, optionally POST `send_text` after
    the `ready` event, and feed every event into `renderer`.

    Mirrors `stream.Run` in the Go pipeline. The "subscribe-first" pattern
    avoids missing early agent events for fast runs.
    """
    if not conversation_id:
        raise ValueError("conversation_id is required")

    final_err: Optional[BaseException] = None
    ready = False
    sent = False

    try:
        async for event in client.stream(conversation_stream_path(conversation_id)):
            if event.type == "ready" and not ready:
                ready = True
                if send_text and not sent:
                    resp = await _send_message(
                        client, conversation_id, send_text,
                        reasoning if mode is None else mode != "instant",
                        mode=mode,
                    )
                    sent = True
                    if on_run_id is not None:
                        result = on_run_id(resp.run_id)
                        if hasattr(result, "__await__"):
                            await result  # type: ignore[func-returns-value]
            if not renderer.render(event):
                break
    except BaseException as e:
        final_err = e
    finally:
        renderer.stop(final_err)

    if final_err is not None:
        raise final_err


# ── generic admin SSE loop ────────────────────────────────────────────────


async def run_admin_stream(
    client: Client,
    path: str,
    mode: OutputMode,
) -> None:
    """Print an admin/snapshot SSE feed line by line.

    Default: `[type] {raw_json}\\n`. With `mode.json`, the raw event JSON
    only. Mirrors the Go `runStream` helper in `cli/cmd/skillevents.go`.
    """
    async for event in client.stream(path):
        if mode.json:
            line = event.raw
            sys.stdout.write(line)
            if not line.endswith("\n"):
                sys.stdout.write("\n")
        else:
            sys.stdout.write(f"[{event.type}] {event.raw}\n")
        sys.stdout.flush()
