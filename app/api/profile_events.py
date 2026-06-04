"""Multiplexed per-profile SSE stream.

Combines notifications, conversations-list, **and** per-conversation
streaming events into a single SSE connection so each browser tab only
holds one slot for all of them — Chrome's HTTP/1.1 6-per-host cap was
being saturated by the chat tab's long-lived streams (one per active
conversation), stalling later requests with "Provisional headers are
shown" once multiple tabs were open.

Frame format uses SSE ``event:`` typing so the client can dispatch by
name:

    event: notification
    data: {<EventNotificationEntry>}

    event: conversations-list
    data: {"conversations": [...]}

    event: conversation-event
    data: {"conversation_id": "...", "seq": N, "type": "text", "data": {...}}

    event: ready
    data: {}

The embedding-state stream (:mod:`app.api.embedding_stream`) is kept
separate — it is intentionally unauthenticated to support the pre-token
setup wizard, while this endpoint is auth-gated and profile-scoped.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.events import (
    get_event_notifications,
    get_notifications_stream_bus,
)
from app.events.conversations_list_bus import get_conversations_list_stream_bus
from app.events.profile_stream_fanout import get_profile_stream_fanout
from app.events.stream_bus import get_event_stream_bus
from app.storage.conversation_storage import ConversationStorage


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _event_frame(event_name: str, data: Any) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def get_profile_events_routes(
    conversation_storage: ConversationStorage,
) -> list[Route]:

    async def _build_conversations_snapshot(
        profile: str, channel_type: str | None,
    ) -> Dict[str, Any]:
        conversations = await conversation_storage.list_conversations(
            profile, limit=500, offset=0, channel_type=channel_type,
        )
        return {"conversations": conversations}

    async def handle_profile_events_stream(request: Request) -> Any:
        """Merged SSE: notifications + conversations-list + per-conversation events.

        Query params mirror the source endpoints:
        - ``since`` (ms) — notifications replay cursor (default ``0``)
        - ``channel_type`` — conversations-list filter; ``all`` is the
          virtual "no filter" sentinel

        On connect, replays buffered notifications, sends one fresh
        conversations-list snapshot, replays the in-progress run for each
        active conversation owned by this profile, emits ``event: ready``,
        then forwards live frames from all three sources.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        since_raw = request.query_params.get("since") or "0"
        try:
            since_ms = float(since_raw)
        except ValueError:
            since_ms = 0.0

        raw_channel_type = request.query_params.get("channel_type") or None
        channel_type = None if raw_channel_type == "all" else raw_channel_type

        notif_bus = get_notifications_stream_bus()
        notif_queue = notif_bus.subscribe(profile)
        convs_bus = get_conversations_list_stream_bus()
        convs_queue = convs_bus.subscribe(profile)
        fanout_bus = get_profile_stream_fanout()
        fanout_queue = await fanout_bus.subscribe(profile)

        replay = get_event_notifications().since(profile, since_ms)
        conv_snapshots = await get_event_stream_bus().snapshot_for_profile(profile)

        async def generator():
            notif_task: asyncio.Task | None = None
            convs_task: asyncio.Task | None = None
            fanout_task: asyncio.Task | None = None
            try:
                for entry in replay:
                    yield _event_frame("notification", entry)
                snapshot = await _build_conversations_snapshot(profile, channel_type)
                yield _event_frame("conversations-list", snapshot)
                # Replay each active conversation's ring buffer so a late
                # subscriber catches the in-progress run, mirroring what
                # /api/conversations/{id}/stream does on its own connect.
                for conv_id, ring in conv_snapshots:
                    for event in ring:
                        yield _event_frame(
                            "conversation-event",
                            {"conversation_id": conv_id, **event},
                        )
                yield _event_frame("ready", {})

                notif_task = asyncio.ensure_future(notif_queue.get())
                convs_task = asyncio.ensure_future(convs_queue.get())
                fanout_task = asyncio.ensure_future(fanout_queue.get())

                while True:
                    done, _pending = await asyncio.wait(
                        [notif_task, convs_task, fanout_task],
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=15.0,
                    )
                    if not done:
                        if await request.is_disconnected():
                            return
                        yield b": keepalive\n\n"
                        continue

                    for task in done:
                        if task is notif_task:
                            entry = task.result()
                            yield _event_frame("notification", entry)
                            notif_task = asyncio.ensure_future(notif_queue.get())
                        elif task is convs_task:
                            _ = task.result()  # signal only; rebuild snapshot
                            fresh = await _build_conversations_snapshot(
                                profile, channel_type,
                            )
                            yield _event_frame("conversations-list", fresh)
                            convs_task = asyncio.ensure_future(convs_queue.get())
                        elif task is fanout_task:
                            envelope = task.result()
                            yield _event_frame("conversation-event", envelope)
                            fanout_task = asyncio.ensure_future(fanout_queue.get())

                    if await request.is_disconnected():
                        return
            finally:
                if notif_task is not None and not notif_task.done():
                    notif_task.cancel()
                if convs_task is not None and not convs_task.done():
                    convs_task.cancel()
                if fanout_task is not None and not fanout_task.done():
                    fanout_task.cancel()
                notif_bus.unsubscribe(profile, notif_queue)
                convs_bus.unsubscribe(profile, convs_queue)
                await fanout_bus.unsubscribe(profile, fanout_queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    return [
        Route(
            "/api/profile-events/stream",
            handle_profile_events_stream,
            methods=["GET"],
        ),
    ]
