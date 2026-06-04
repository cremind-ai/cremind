"""Live Setup Wizard progress stream (SSE).

The wizard subscribes to this stream before posting to
``/api/config/setup`` and renders each ``event: log`` frame in its
status panel. Each frame carries:

    {"step": "features|database|vectorstore|profile|...",
     "message": "Installing optional features: postgres",
     "level": "info|success|warning|error",
     "ts": 1700000000.0}

Pre-storage and unauthenticated during the bootstrap window (matches
``/api/features/install``); admin-gated once setup is complete so a
running wizard cannot leak progress to anonymous clients post-setup.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.events.setup_progress_bus import get_setup_progress_bus
from app.runtime import get_state


def get_setup_stream_routes() -> list[Route]:

    async def handle_setup_stream(request: Request) -> Any:
        """SSE stream of setup-wizard progress events.

        Each published bus entry is emitted as one ``event: log`` frame.
        Keepalive every 15s when idle. The stream does not auto-close on
        ``done`` — the client decides when to disconnect.
        """
        state = get_state()
        setup_complete = state.storage_ready and state.config_storage.is_setup_complete()
        if setup_complete:
            denied = require_admin(request)
            if denied is not None:
                return denied

        bus = get_setup_progress_bus()
        queue = bus.subscribe()

        async def generator():
            def _frame(event_name: str, payload: Dict[str, Any]) -> bytes:
                return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                yield _frame("ready", {})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame("log", entry)
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
        Route("/api/config/setup/stream", handle_setup_stream, methods=["GET"]),
    ]
