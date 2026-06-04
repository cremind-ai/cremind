"""Skill Event API.

REST endpoints for the conversation-scoped event subscription system:

- list / delete subscriptions
- simulate an event (drop a markdown file into the watched folder)
- discover events declared by a skill
- check / start a skill's listener daemon
- stream event-driven notifications for a profile over SSE
- stream live events for a conversation over SSE
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.events import (
    get_event_manager,
    get_event_notifications,
    get_event_stream_bus,
    get_notifications_stream_bus,
)
from app.events.skill_events_admin_bus import get_skill_events_admin_stream_bus
from app.storage import (
    get_autostart_storage,
    get_conversation_storage,
    get_event_subscription_storage,
)
from app.tools.builtin.exec_shell import publish_process_list_changed
from app.tools.builtin.exec_shell_autostart import spawn_from_autostart
from app.tools.builtin.register_skill_event import _resolve_skill_source
from app.utils.logger import logger


# Default IMAP poll interval is 30s (see email-cli config). Heartbeat-based
# liveness uses a generous 3× window so a single missed beat doesn't flag down.
_DEFAULT_HEARTBEAT_WINDOW_S = 90.0


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


async def _list_subscriptions_for_profile(profile: str) -> list[Dict[str, Any]]:
    store = get_event_subscription_storage()
    rows = store.list_by_profile(profile)
    if not rows:
        return []
    conv_storage = get_conversation_storage()
    titles: Dict[str, str] = {}
    for row in rows:
        cid = row["conversation_id"]
        if cid in titles:
            continue
        try:
            conv = await conv_storage.get_conversation(cid)
        except Exception:  # noqa: BLE001
            conv = None
        titles[cid] = (conv or {}).get("title") or "Untitled Chat"
    enriched = []
    for row in rows:
        enriched.append({**row, "conversation_title": titles.get(row["conversation_id"], "")})
    return enriched


def _heartbeat_path(source_dir: str) -> Path:
    return Path(source_dir) / "scripts" / ".listener_heartbeat"


def _listener_command(source_dir: str) -> str:
    """Return the canonical autostart command for a skill's listener daemon."""
    script = Path(source_dir) / "scripts" / "event_listener.py"
    return f'uv run "{script}"'


def _build_listener_status(profile: str, skill_name: str) -> Optional[Dict[str, Any]]:
    """Compute the JSON payload returned by ``GET /api/skills/.../listener-status``.

    Returns ``None`` when the skill source can't be resolved (matches the
    handler's 404 path). Pulled out of ``handle_listener_status`` so the SSE
    endpoint can rebuild snapshots without going through HTTP.
    """
    source_dir = _resolve_skill_source(skill_name, profile)
    if not source_dir:
        return None
    hb = _heartbeat_path(source_dir)
    last_heartbeat: Optional[float] = None
    running = False
    if hb.exists():
        try:
            last_heartbeat = hb.stat().st_mtime
        except OSError:
            last_heartbeat = None
    if last_heartbeat is not None:
        running = (time.time() - last_heartbeat) < _DEFAULT_HEARTBEAT_WINDOW_S

    command = _listener_command(source_dir)
    autostart_storage = get_autostart_storage()
    autostart_row = autostart_storage.find_duplicate(profile, command)
    return {
        "skill_name": skill_name,
        "running": running,
        "last_heartbeat": last_heartbeat,
        "autostart_id": (autostart_row or {}).get("id"),
        "command": command,
    }


async def _build_skill_events_admin_snapshot(profile: str) -> Dict[str, Any]:
    """Bundle subscriptions + per-skill listener statuses for the events page.

    Mirrors the two REST calls the page used to make on every refresh — the
    SSE stream just hands back the same shape on every push so the client
    can replace its state in one go.
    """
    subscriptions = await _list_subscriptions_for_profile(profile)
    listeners: Dict[str, Dict[str, Any]] = {}
    seen: set[str] = set()
    for sub in subscriptions:
        name = sub["skill_name"]
        if name in seen:
            continue
        seen.add(name)
        try:
            status = _build_listener_status(profile, name)
        except Exception:  # noqa: BLE001
            status = None
        if status is not None:
            listeners[name] = status
    return {"subscriptions": subscriptions, "listeners": listeners}


def publish_skill_events_admin_changed(profile: Optional[str]) -> None:
    """Tell the events-page SSE endpoint to rebuild and push a snapshot.

    The bus carries no payload — building the snapshot needs async DB calls
    that don't belong in publish callsites. Subscribers wake on the tick and
    rebuild themselves.
    """
    if not profile:
        return
    try:
        get_skill_events_admin_stream_bus().publish(profile, {})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"skill-events admin bus publish failed: {exc}")


def get_event_routes() -> list[Route]:

    async def handle_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        subs = await _list_subscriptions_for_profile(profile)
        return JSONResponse({"subscriptions": subs})

    async def handle_delete(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        sub_id = request.path_params["id"]
        store = get_event_subscription_storage()
        existing = store.get(sub_id)
        if existing is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and existing["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        store.delete(sub_id)
        try:
            get_event_manager().release_watcher(
                profile=existing["profile"],
                skill_name=existing["skill_name"],
                event_type=existing["event_type"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to release watcher after delete")
        publish_skill_events_admin_changed(profile)
        return JSONResponse({"ok": True})

    async def handle_simulate(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        sub_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        content = body.get("content") or ""
        if not content.strip():
            return JSONResponse(
                {"error": "Missing parameter", "message": "content is required"},
                status_code=400,
            )
        # Optional user-supplied filename. Default to a unique simulate-*.md.
        # Sanitize: strip directory components, allow only basename, force .md.
        raw_name = (body.get("filename") or "").strip()
        if raw_name:
            safe_name = Path(raw_name).name  # strip any path separators
            # Disallow empty / dotfile-only / Windows-reserved characters.
            forbidden = set('<>:"/\\|?*\0')
            if not safe_name or safe_name in (".", "..") or any(
                c in forbidden for c in safe_name
            ):
                return JSONResponse(
                    {
                        "error": "invalid_filename",
                        "message": "filename contains illegal characters or is empty",
                    },
                    status_code=400,
                )
            if not safe_name.lower().endswith(".md"):
                safe_name = f"{safe_name}.md"
            filename = safe_name
        else:
            filename = f"simulate-{int(time.time() * 1000)}.md"
        existing = get_event_subscription_storage().get(sub_id)
        if existing is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and existing["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        source_dir = _resolve_skill_source(existing["skill_name"], profile)
        if not source_dir:
            return JSONResponse(
                {"error": "Skill source not found"}, status_code=404,
            )
        events_dir = Path(source_dir) / "events" / existing["event_type"]
        try:
            events_dir.mkdir(parents=True, exist_ok=True)
            file_path = events_dir / filename
            # Avoid clobbering a same-named file already waiting to fire.
            if file_path.exists():
                stem, suffix = file_path.stem, file_path.suffix
                file_path = events_dir / f"{stem}-{int(time.time() * 1000)}{suffix}"
            file_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return JSONResponse(
                {"error": "write_failed", "message": str(exc)}, status_code=500,
            )
        return JSONResponse({"ok": True, "path": str(file_path)})

    async def handle_skill_events(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        skill_name = request.path_params["skill_name"]
        source_dir = _resolve_skill_source(skill_name, profile)
        if not source_dir:
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        from app.tools.builtin.register_skill_event import _read_events_metadata

        events = _read_events_metadata(Path(source_dir))
        return JSONResponse(
            {"skill_name": skill_name, "source_dir": source_dir, "events": events}
        )

    async def handle_listener_status(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        skill_name = request.path_params["skill_name"]
        status = _build_listener_status(profile, skill_name)
        if status is None:
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        return JSONResponse(status)

    async def handle_listener_start(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        skill_name = request.path_params["skill_name"]
        source_dir = _resolve_skill_source(skill_name, profile)
        if not source_dir:
            return JSONResponse({"error": "Skill not found"}, status_code=404)
        command = _listener_command(source_dir)
        scripts_dir = str(Path(source_dir) / "scripts")
        autostart_storage = get_autostart_storage()
        row = autostart_storage.find_duplicate(profile, command)
        if row is None:
            row = autostart_storage.insert(
                profile=profile,
                command=command,
                working_dir=scripts_dir,
                is_pty=False,
            )
            publish_process_list_changed(profile)
        process_id, error = await spawn_from_autostart(row)
        if error:
            autostart_storage.set_error(row["id"], error)
            publish_process_list_changed(profile)
            publish_skill_events_admin_changed(profile)
            return JSONResponse(
                {"error": "spawn_failed", "message": error}, status_code=400,
            )
        autostart_storage.clear_error(row["id"])
        publish_skill_events_admin_changed(profile)
        return JSONResponse(
            {"ok": True, "process_id": process_id, "autostart_id": row["id"]}
        )

    async def handle_conversation_stream(request: Request) -> Any:
        """Server-Sent Events endpoint streaming live agent runs for a conversation.

        Subscribes to the in-memory :class:`ConversationStreamBus`. The first
        events yielded are a replay of the in-progress run (if any), so a UI
        client opening the conversation mid-stream catches up before the live
        tail begins. A ``ready`` marker separates replay from live events.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["id"]

        try:
            conv = await get_conversation_storage().get_conversation(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception("Stream endpoint: failed to load conversation")
            return JSONResponse({"error": "Lookup failed"}, status_code=500)
        if conv is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and conv.get("profile") != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        bus = get_event_stream_bus()
        queue, replay, is_active = await bus.subscribe(conversation_id)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                for ev in replay:
                    yield _frame(ev)
                # Seed the conversation's effective working directory so
                # clients can render the file-tree pane immediately without
                # an extra round-trip. Hydrate from the persisted column
                # first so reopening a conversation after a restart
                # restores the user's last-chosen directory; if the
                # persisted path no longer exists, the helper clears it
                # and we fall back to the user default.
                from app.utils.working_directory import hydrate_working_directory
                effective_cwd = await hydrate_working_directory(
                    conversation_id, get_conversation_storage(),
                )
                yield _frame({
                    "type": "ready",
                    "data": {
                        "is_active": is_active,
                        "working_directory": effective_cwd,
                    },
                })

                # Heartbeat so the client/proxy keeps the connection open
                # during quiet periods (no events for a while).
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # SSE comment line — ignored by clients, keeps proxies happy.
                        yield b": keepalive\n\n"
                        continue
                    yield _frame(ev)
            finally:
                await bus.unsubscribe(conversation_id, queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    async def handle_notifications_stream(request: Request) -> Any:
        """SSE endpoint streaming live skill-event notifications for the caller's profile.

        On connect, the client may pass ``?since=<ms>`` as a cursor — entries
        in the buffer with ``created_at > since`` are replayed first (catching
        the client up after a transient disconnect), followed by a ``ready``
        marker and then the live tail. With ``since=0`` (or omitted), the full
        per-profile ring buffer is replayed.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)

        since_raw = request.query_params.get("since") or "0"
        try:
            since_ms = float(since_raw)
        except ValueError:
            since_ms = 0.0

        bus = get_notifications_stream_bus()
        queue = bus.subscribe(profile)
        replay = get_event_notifications().since(profile, since_ms)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                for entry in replay:
                    yield _frame({"type": "notification", "data": entry})
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame({"type": "notification", "data": entry})
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

    async def handle_admin_stream(request: Request) -> Any:
        """SSE endpoint pushing the live events-page snapshot for the caller's profile.

        On connect, sends a ``snapshot`` frame containing the same payload
        the page used to GET as two separate REST calls
        (``/api/skill-events`` + ``/api/skills/{name}/listener-status``),
        followed by a ``ready`` marker. Subsequent ``snapshot`` frames are
        emitted whenever a subscription is created or deleted, or a listener
        is started — anything else (heartbeat-driven liveness flips) is
        deferred to the next deterministic event.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)

        bus = get_skill_events_admin_stream_bus()
        queue = bus.subscribe(profile)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                snapshot = await _build_skill_events_admin_snapshot(profile)
                yield _frame({"type": "snapshot", "data": snapshot})
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    snapshot = await _build_skill_events_admin_snapshot(profile)
                    yield _frame({"type": "snapshot", "data": snapshot})
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
        Route("/api/skill-events", handle_list, methods=["GET"]),
        Route(
            "/api/skill-events/admin/stream",
            handle_admin_stream, methods=["GET"],
        ),
        Route("/api/skill-events/{id}", handle_delete, methods=["DELETE"]),
        Route("/api/skill-events/{id}/simulate", handle_simulate, methods=["POST"]),
        Route("/api/skills/{skill_name}/events", handle_skill_events, methods=["GET"]),
        Route(
            "/api/skills/{skill_name}/listener-status",
            handle_listener_status, methods=["GET"],
        ),
        Route(
            "/api/skills/{skill_name}/listener-start",
            handle_listener_start, methods=["POST"],
        ),
        Route(
            "/api/skill-events/notifications/stream",
            handle_notifications_stream, methods=["GET"],
        ),
        Route(
            "/api/conversations/{id}/stream",
            handle_conversation_stream, methods=["GET"],
        ),
    ]
