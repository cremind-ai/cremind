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

    event: settings-state
    data: {}

    event: processes
    data: {"processes": [...]}

    event: embedding-state
    data: {"status": ..., "phase": ..., "error": ..., "ready": bool,
           "busy": bool, "enabled": bool}

    event: ready
    data: {}

Folding the settings-state, processes, and embedding-state streams into
this connection keeps an authenticated web session down to a single SSE
socket, so opening several tabs no longer exhausts Chrome's HTTP/1.1
6-per-origin cap. The standalone endpoints (:mod:`app.api.settings_stream`,
:mod:`app.api.processes`, :mod:`app.api.embedding_stream`) remain: the CLI
consumes them directly, and the embedding one is intentionally
unauthenticated so the pre-token setup wizard can subscribe before a
profile (and therefore this auth-gated stream) exists.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api.embedding_stream import _augment_with_enabled
from app.events import (
    get_event_notifications,
    get_notifications_stream_bus,
)
from app.events.conversations_list_bus import get_conversations_list_stream_bus
from app.events.embedding_state_bus import get_embedding_state_stream_bus
from app.events.processes_bus import get_processes_stream_bus
from app.events.profile_stream_fanout import get_profile_stream_fanout
from app.events.settings_state_bus import get_settings_state_stream_bus
from app.events.stream_bus import get_event_stream_bus
from app.storage.conversation_storage import ConversationStorage
from app.tools.builtin.exec_shell import list_processes


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
        active conversation owned by this profile, emits an initial
        settings/processes/embedding snapshot, emits ``event: ready``,
        then forwards live frames from all sources.
        """
        from app.config.embedding_state import embedding_state

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
        # Folded-in sources — subscribe before streaming so no mutation is
        # missed between the connect-time snapshot and the live tail.
        settings_bus = get_settings_state_stream_bus()
        settings_queue = settings_bus.subscribe(profile)
        proc_bus = get_processes_stream_bus()
        proc_queue = proc_bus.subscribe(profile)
        # Embedding is a single process-wide resource — the bus is not
        # profile-keyed, so every subscriber sees the same stream.
        emb_bus = get_embedding_state_stream_bus()
        emb_queue = emb_bus.subscribe()

        replay = get_event_notifications().since(profile, since_ms)
        conv_snapshots = await get_event_stream_bus().snapshot_for_profile(profile)

        async def generator():
            notif_task: asyncio.Task | None = None
            convs_task: asyncio.Task | None = None
            fanout_task: asyncio.Task | None = None
            settings_task: asyncio.Task | None = None
            proc_task: asyncio.Task | None = None
            emb_task: asyncio.Task | None = None
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
                # Connect-time snapshots for the folded-in sources, mirroring
                # each standalone endpoint's own on-connect frame.
                yield _event_frame("settings-state", {})
                yield _event_frame("processes", {"processes": list_processes(profile)})
                yield _event_frame(
                    "embedding-state", _augment_with_enabled(embedding_state.to_dict()),
                )
                yield _event_frame("ready", {})

                notif_task = asyncio.ensure_future(notif_queue.get())
                convs_task = asyncio.ensure_future(convs_queue.get())
                fanout_task = asyncio.ensure_future(fanout_queue.get())
                settings_task = asyncio.ensure_future(settings_queue.get())
                proc_task = asyncio.ensure_future(proc_queue.get())
                emb_task = asyncio.ensure_future(emb_queue.get())

                while True:
                    done, _pending = await asyncio.wait(
                        [
                            notif_task, convs_task, fanout_task,
                            settings_task, proc_task, emb_task,
                        ],
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
                        elif task is settings_task:
                            _ = task.result()  # wakeup only; client refetches
                            yield _event_frame("settings-state", {})
                            settings_task = asyncio.ensure_future(settings_queue.get())
                        elif task is proc_task:
                            # Bus entry is already the full {"processes": [...]}
                            # snapshot — forward as-is.
                            entry = task.result()
                            yield _event_frame("processes", entry)
                            proc_task = asyncio.ensure_future(proc_queue.get())
                        elif task is emb_task:
                            # Live bus frames lack the persisted ``enabled``
                            # flag — splice it in, as the standalone endpoint does.
                            entry = task.result()
                            yield _event_frame(
                                "embedding-state", _augment_with_enabled(entry),
                            )
                            emb_task = asyncio.ensure_future(emb_queue.get())

                    if await request.is_disconnected():
                        return
            finally:
                for task in (
                    notif_task, convs_task, fanout_task,
                    settings_task, proc_task, emb_task,
                ):
                    if task is not None and not task.done():
                        task.cancel()
                notif_bus.unsubscribe(profile, notif_queue)
                convs_bus.unsubscribe(profile, convs_queue)
                await fanout_bus.unsubscribe(profile, fanout_queue)
                settings_bus.unsubscribe(profile, settings_queue)
                proc_bus.unsubscribe(profile, proc_queue)
                emb_bus.unsubscribe(emb_queue)

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
