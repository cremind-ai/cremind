"""Live settings-page state stream (SSE).

Wakeup-only stream signalling that any of the resources rendered on the
Settings → Tools & Skills page or in the Agents drawer has changed:

- tools / skills (enable, config, register/unregister, mode)
- remote agents (enable/disable, register, auth status)
- LLM providers (config, models)
- setup status
- skill mode

The bus payload is empty — the client refetches the resources it cares
about (via the existing REST endpoints) on each wakeup. This avoids
having to bundle five snapshot builders into the stream handler while
still delivering real-time updates to every consumer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.events.settings_state_bus import get_settings_state_stream_bus


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def get_settings_stream_routes() -> list[Route]:

    async def handle_settings_stream(request: Request) -> Any:
        """SSE endpoint pinging the caller's profile when any settings-page
        resource changes. Each frame is a wakeup; clients refetch the
        resources they render.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        bus = get_settings_state_stream_bus()
        queue = bus.subscribe(profile)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                # Initial ping so the client triggers its first refetch.
                yield _frame({"type": "changed", "data": {}})
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame({"type": "changed", "data": {}})
            finally:
                bus.unsubscribe(profile, queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    return [
        Route("/api/settings/state/stream", handle_settings_stream, methods=["GET"]),
    ]
