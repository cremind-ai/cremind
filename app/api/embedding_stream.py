"""Live vector-embedding state stream (SSE).

Server-wide topic — there's a single embedding model per process, so all
subscribers see the same stream. Each frame carries the full state
snapshot:

    {"type": "state", "data": {
        "status": "ready" | "initializing" | "rebuilding" | "failed" | "disabled",
        "phase": "loading_model" | "connecting_store" | ...,
        "error": null | "...",
        "ready": bool,
        "busy": bool,
        "enabled": bool,
    }}

Mirrors the structure of :mod:`app.api.settings_stream` but server-wide
and unauthenticated — the setup wizard subscribes pre-token.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from app.events.embedding_state_bus import get_embedding_state_stream_bus


def _augment_with_enabled(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Add the persisted ``enabled`` flag to a state snapshot.

    The state singleton tracks runtime status only; whether embedding is
    *configured* enabled is read from ``BaseConfig.is_embedding_enabled()``.
    The UI needs both, so we splice them together here.
    """
    from app.config.settings import BaseConfig
    out = dict(snapshot)
    out["enabled"] = BaseConfig.is_embedding_enabled()
    return out


def get_embedding_stream_routes() -> list[Route]:

    async def handle_embedding_stream(request: Request) -> Any:
        """SSE stream of vector-embedding lifecycle state.

        On connect: emits the current state, then a ``ready`` frame so
        the client knows the channel is live. Subsequent state changes
        are pushed inline. Keepalive every 15s when idle.
        """
        from app.config.embedding_state import embedding_state

        bus = get_embedding_state_stream_bus()
        queue = bus.subscribe()

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                # Initial snapshot so a freshly-connected client doesn't
                # wait for the next mutation to render the badge.
                yield _frame({
                    "type": "state",
                    "data": _augment_with_enabled(embedding_state.to_dict()),
                })
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        snapshot = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame({
                        "type": "state",
                        "data": _augment_with_enabled(snapshot),
                    })
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
        Route("/api/config/embedding/stream", handle_embedding_stream, methods=["GET"]),
    ]
