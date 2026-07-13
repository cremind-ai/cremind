"""Server-Sent Events iterator.

Mirrors `cli/internal/client/sse.go`. The Cremind server emits frames of the
shape `data: {"type": "...", ...}\\n\\n` plus `: keepalive` comments on idle
ticks. There is no `Last-Event-ID` resume — clients reconnect from scratch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from httpx_sse import aconnect_sse

from app.cli.client._base import APIError


@dataclass(frozen=True)
class Event:
    """A single SSE frame's decoded JSON payload.

    `type` is read from the top-level `type` field for convenience. `data`
    is the full parsed dict, and `raw` is the original `data:` line
    (concatenated when split across multiple lines) — useful for forwarding
    payloads verbatim with `--json`.

    `event` is the SSE frame's event *name* (the `event:` field). Almost every
    Cremind stream uses data-only frames whose payload carries its own `type`
    key, so `event` is the httpx_sse default `"message"` there. The exception
    is `/api/features/install`, which emits *named* frames (`event: log` /
    `done` / `error`) with no top-level `type` — those commands branch on
    `event` instead.
    """

    type: str
    data: dict[str, Any]
    raw: str
    event: str = ""


async def _raise_for_status(response: httpx.Response) -> None:
    """Buffer the body and raise APIError when the stream response is non-2xx."""
    if response.status_code < 400:
        return
    content = await response.aread()
    body = ""
    if content:
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and isinstance(decoded.get("error"), str):
            body = decoded["error"]
        else:
            body = content.decode(errors="replace").strip()
    raise APIError(status=response.status_code, body=body, raw=content)


def _decode(sse: Any) -> "Event | None":
    """Decode one raw SSE frame into an `Event`, or None to skip it."""
    if not sse.data:
        return None
    try:
        parsed = json.loads(sse.data)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return Event(
        type=str(parsed.get("type", "")),
        data=parsed,
        raw=sse.data,
        event=str(getattr(sse, "event", "") or ""),
    )


async def stream_events(
    client: httpx.AsyncClient,
    path: str,
) -> AsyncIterator[Event]:
    """Open an SSE GET stream against `path` and yield `Event`s."""
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
    async with aconnect_sse(client, "GET", path, headers=headers) as event_source:
        await _raise_for_status(event_source.response)
        async for sse in event_source.aiter_sse():
            event = _decode(sse)
            if event is not None:
                yield event


async def stream_events_post(
    client: httpx.AsyncClient,
    path: str,
    body: Any,
) -> AsyncIterator[Event]:
    """Open an SSE POST stream against `path` (JSON `body`) and yield `Event`s.

    Some streaming endpoints (`/api/features/install`) are POST-only — they
    take a request body — so a plain GET stream can't reach them.
    """
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
    async with aconnect_sse(
        client, "POST", path, json=body, headers=headers,
    ) as event_source:
        await _raise_for_status(event_source.response)
        async for sse in event_source.aiter_sse():
            event = _decode(sse)
            if event is not None:
                yield event
