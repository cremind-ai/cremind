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
    """

    type: str
    data: dict[str, Any]
    raw: str


async def stream_events(
    client: httpx.AsyncClient,
    path: str,
) -> AsyncIterator[Event]:
    """Open an SSE GET stream against `path` and yield `Event`s."""
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
    async with aconnect_sse(client, "GET", path, headers=headers) as event_source:
        response = event_source.response
        if response.status_code >= 400:
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

        async for sse in event_source.aiter_sse():
            if not sse.data:
                continue
            try:
                parsed = json.loads(sse.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            yield Event(
                type=str(parsed.get("type", "")),
                data=parsed,
                raw=sse.data,
            )
