"""Live server-log stream (SSE) for the Developer page.

Admin-only. On connect, the handler replays the most recent records
held in the log stream bus's ring buffer, emits a ``ready`` frame, and
then forwards every subsequent record live. Filtering by level happens
on the client — the server sends everything.

Frame shape mirrors :mod:`app.api.embedding_stream`:

    {"type": "log", "data": {"ts": ..., "level": ..., "source": ..., "message": ...}}
    {"type": "ready"}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.events.log_stream_bus import get_log_stream_bus


def get_logs_stream_routes() -> list[Route]:

    async def handle_logs_stream(request: Request) -> Any:
        denied = require_admin(request)
        if denied is not None:
            return denied

        bus = get_log_stream_bus()
        queue, backfill = bus.subscribe()

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                for entry in backfill:
                    yield _frame({"type": "log", "data": entry})
                yield _frame({"type": "ready"})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame({"type": "log", "data": entry})
            finally:
                bus.unsubscribe(queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    return [
        Route("/api/server/logs/stream", handle_logs_stream, methods=["GET"]),
    ]
