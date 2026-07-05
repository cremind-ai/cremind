"""Combined admin-page SSE stream.

Multiplexes the skill-events-admin and file-watcher-admin snapshot streams
onto a single SSE connection. The Events page (``SkillEventsPage`` +
``FileWatcherSection``) renders both, and previously opened **two**
long-lived connections for them. Each browser origin can hold only ~6
concurrent HTTP/1.1 connections, and those two streams stacked on top of
the embedding / profile-events streams the app already keeps open — once a
couple of tabs were involved the pool saturated and later REST requests
stalled with Chrome's "Provisional headers are shown". One connection
carries both snapshots now.

Frame format mirrors :mod:`app.api.profile_events` — SSE ``event:`` typing
so the client can dispatch by name:

    event: skill-events
    data: {"subscriptions": [...], "listeners": {...}}

    event: file-watchers
    data: {"subscriptions": [...]}

    event: ready
    data: {}

Like the two source streams it replaces, this endpoint is auth-gated and
profile-scoped (the "admin" name refers to the management page, not the
admin profile).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api.calendar import build_schedule_events_admin_snapshot
from app.api.event_runs import build_event_runs_admin_snapshot
from app.api.events import _build_skill_events_admin_snapshot
from app.api.file_watchers import (
    _build_admin_snapshot as _build_file_watcher_admin_snapshot,
)
from app.events.event_runs_admin_bus import get_event_runs_admin_stream_bus
from app.events.file_watcher_admin_bus import get_file_watcher_admin_stream_bus
from app.events.schedule_events_admin_bus import get_schedule_events_admin_stream_bus
from app.events.skill_events_admin_bus import get_skill_events_admin_stream_bus


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _event_frame(event_name: str, data: Any) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


def get_admin_stream_routes() -> list[Route]:

    async def handle_admin_events_stream(request: Request) -> Any:
        """Merged SSE: skill-events-admin + file-watcher-admin snapshots.

        On connect, sends one snapshot of each (``skill-events`` then
        ``file-watchers``) followed by a ``ready`` marker, then re-emits the
        relevant snapshot whenever either admin bus signals a change.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)

        se_bus = get_skill_events_admin_stream_bus()
        se_queue = se_bus.subscribe(profile)
        fw_bus = get_file_watcher_admin_stream_bus()
        fw_queue = fw_bus.subscribe(profile)
        sch_bus = get_schedule_events_admin_stream_bus()
        sch_queue = sch_bus.subscribe(profile)
        er_bus = get_event_runs_admin_stream_bus()
        er_queue = er_bus.subscribe(profile)

        async def generator():
            se_task: asyncio.Task | None = None
            fw_task: asyncio.Task | None = None
            sch_task: asyncio.Task | None = None
            er_task: asyncio.Task | None = None
            try:
                yield _event_frame(
                    "skill-events",
                    await _build_skill_events_admin_snapshot(profile),
                )
                yield _event_frame(
                    "file-watchers",
                    await _build_file_watcher_admin_snapshot(profile),
                )
                yield _event_frame(
                    "schedule-events",
                    await build_schedule_events_admin_snapshot(profile),
                )
                yield _event_frame(
                    "event-runs",
                    await build_event_runs_admin_snapshot(profile),
                )
                yield _event_frame("ready", {})

                se_task = asyncio.ensure_future(se_queue.get())
                fw_task = asyncio.ensure_future(fw_queue.get())
                sch_task = asyncio.ensure_future(sch_queue.get())
                er_task = asyncio.ensure_future(er_queue.get())

                while True:
                    done, _pending = await asyncio.wait(
                        [se_task, fw_task, sch_task, er_task],
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=15.0,
                    )
                    if not done:
                        if await request.is_disconnected():
                            return
                        yield b": keepalive\n\n"
                        continue

                    for task in done:
                        if task is se_task:
                            _ = task.result()  # signal only; rebuild snapshot
                            yield _event_frame(
                                "skill-events",
                                await _build_skill_events_admin_snapshot(profile),
                            )
                            se_task = asyncio.ensure_future(se_queue.get())
                        elif task is fw_task:
                            _ = task.result()
                            yield _event_frame(
                                "file-watchers",
                                await _build_file_watcher_admin_snapshot(profile),
                            )
                            fw_task = asyncio.ensure_future(fw_queue.get())
                        elif task is sch_task:
                            _ = task.result()
                            yield _event_frame(
                                "schedule-events",
                                await build_schedule_events_admin_snapshot(profile),
                            )
                            sch_task = asyncio.ensure_future(sch_queue.get())
                        elif task is er_task:
                            _ = task.result()
                            yield _event_frame(
                                "event-runs",
                                await build_event_runs_admin_snapshot(profile),
                            )
                            er_task = asyncio.ensure_future(er_queue.get())

                    if await request.is_disconnected():
                        return
            finally:
                for t in (se_task, fw_task, sch_task, er_task):
                    if t is not None and not t.done():
                        t.cancel()
                se_bus.unsubscribe(profile, se_queue)
                fw_bus.unsubscribe(profile, fw_queue)
                sch_bus.unsubscribe(profile, sch_queue)
                er_bus.unsubscribe(profile, er_queue)

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
            "/api/admin/events-stream",
            handle_admin_events_stream,
            methods=["GET"],
        ),
    ]
