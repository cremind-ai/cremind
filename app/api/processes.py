"""Process Manager API.

REST endpoints list / stop / interact with long-running ``exec_shell``
processes.  A WebSocket endpoint streams their live stdout/stderr and
accepts stdin + PTY resize messages from the UI.

The agent-facing tools (``ExecShellInputTool`` / ``ExecShellOutputTool`` /
``ExecShellStopTool``) are unchanged — they delegate into the same helper
functions this module uses.  The log-writer's file output is untouched.
This API is a pure side channel.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.config.settings import BaseConfig
from app.storage import get_autostart_storage
from app.tools.builtin.exec_shell import (
    list_processes,
    process_status,
    publish_process_list_changed,
    resize_pty,
    stop_process,
    subscribe,
    unsubscribe,
    write_stdin_to_process,
    _DEFAULT_CLEANUP_TTL_HOURS,
    _process_registry,
)
from app.events.processes_bus import get_processes_stream_bus
from starlette.responses import StreamingResponse
from app.tools.builtin.exec_shell_autostart import spawn_from_autostart
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    """Return the authenticated profile name.

    Tokens in this app have ``sub == profile`` (see
    ``app/api/config.py:_generate_token``), so ``request.user.username``
    (populated from the ``sub`` claim by ``JWTAuthBackend``) is the profile.
    """
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    """Return a 401 response if the request is unauthenticated, else None."""
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _decode_ws_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode the JWT used for WebSocket auth.

    Mirrors ``JWTAuthBackend.authenticate``.  Returns the decoded payload
    (a dict) on success, ``None`` on any failure.  Callers should treat
    any ``None`` result as "close with 1008".
    """
    secret = BaseConfig.get_jwt_secret()
    if not secret or not token:
        return None
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


def get_process_routes() -> list:
    """Collect the Process Manager HTTP + WebSocket routes."""

    async def handle_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        return JSONResponse({"processes": list_processes(profile)})

    async def handle_get(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        pid = request.path_params["pid"]
        info = _process_registry.get(pid)
        if info is None:
            return JSONResponse({"error": "Process not found"}, status_code=404)
        if profile and info.profile and info.profile != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        status, exit_code = process_status(info)
        return JSONResponse({
            "process_id": pid,
            "command": info.command,
            "working_dir": info.working_dir,
            "log_dir": info.log_dir,
            "status": status,
            "exit_code": exit_code,
            "created_at": info.created_at,
            "is_pty": info.is_pty,
        })

    async def handle_stop(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        pid = request.path_params["pid"]
        result = await stop_process(pid, profile=profile)
        status_code = 200
        if result.get("error") == "Process not found":
            status_code = 404
        elif result.get("error") == "Forbidden":
            status_code = 403
        elif result.get("error"):
            status_code = 400
        return JSONResponse(result, status_code=status_code)

    async def handle_stdin(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        pid = request.path_params["pid"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        result = await write_stdin_to_process(
            pid,
            profile=profile,
            mode=body.get("mode"),
            input_text=body.get("input_text"),
            keys=body.get("keys"),
            line_ending=body.get("line_ending"),
        )
        status_code = 200
        if result.get("error") == "Process not found":
            status_code = 404
        elif result.get("error") == "Forbidden":
            status_code = 403
        elif result.get("error"):
            status_code = 400
        return JSONResponse(result, status_code=status_code)

    async def handle_resize(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        pid = request.path_params["pid"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        try:
            cols = int(body.get("cols"))
            rows = int(body.get("rows"))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "cols and rows must be integers"}, status_code=400,
            )
        result = await resize_pty(pid, cols, rows, profile=profile)
        status_code = 200 if result.get("ok") else 400
        if result.get("error") == "Process not found":
            status_code = 404
        elif result.get("error") == "Forbidden":
            status_code = 403
        return JSONResponse(result, status_code=status_code)

    async def handle_ws(websocket: WebSocket) -> None:
        # Browsers can't set Authorization on the native WebSocket
        # constructor, so the client passes the token via the
        # Sec-WebSocket-Protocol subprotocol header as ['bearer', <token>].
        # We echo 'bearer' back so the handshake completes.
        subprotocols = list(websocket.scope.get("subprotocols") or [])
        token: Optional[str] = None
        if len(subprotocols) >= 2 and subprotocols[0] == "bearer":
            token = subprotocols[1]
        payload = _decode_ws_token(token or "")
        if payload is None:
            await websocket.close(code=1008)
            return

        profile = payload.get("profile") or payload.get("sub") or ""
        pid = websocket.path_params.get("pid") or ""

        info = _process_registry.get(pid)
        if info is None:
            await websocket.close(code=1008)
            return
        if profile and info.profile and info.profile != profile:
            await websocket.close(code=1008)
            return

        try:
            queue, snapshot = await subscribe(pid)
        except KeyError:
            await websocket.close(code=1008)
            return

        await websocket.accept(subprotocol="bearer")

        try:
            # Send initial snapshot + status.  Frontend writes snapshot
            # chunks in order, which reproduces the most recent ~ring-
            # buffer-max bytes of output in the xterm.js scrollback.
            await websocket.send_json({
                "type": "snapshot",
                "chunks": [
                    {"type": stream, "data": data} for stream, data in snapshot
                ],
            })
            status, exit_code = process_status(info)
            await websocket.send_json({
                "type": "status",
                "data": {
                    "process_id": pid,
                    "command": info.command,
                    "working_dir": info.working_dir,
                    "is_pty": info.is_pty,
                    "status": status,
                    "exit_code": exit_code,
                },
            })

            async def pump_to_client() -> None:
                while True:
                    message = await queue.get()
                    if message.get("type") == "overflow":
                        try:
                            await websocket.send_json(message)
                        finally:
                            await websocket.close(code=1011)
                        return
                    await websocket.send_json(message)

            async def pump_from_client() -> None:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    msg_type = message.get("type")
                    if msg_type == "stdin":
                        await write_stdin_to_process(
                            pid,
                            profile=profile,
                            input_text=message.get("data"),
                            line_ending=message.get(
                                "line_ending", "none",
                            ),
                        )
                    elif msg_type == "resize":
                        try:
                            cols = int(message.get("cols"))
                            rows = int(message.get("rows"))
                        except (TypeError, ValueError):
                            continue
                        await resize_pty(pid, cols, rows, profile=profile)
                    elif msg_type == "ping":
                        pass
                    # Unknown types are silently dropped.

            producer = asyncio.create_task(pump_to_client())
            consumer = asyncio.create_task(pump_from_client())
            done, pending = await asyncio.wait(
                {producer, consumer},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    logger.debug(f"process ws task ended with {exc!r}")
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug(f"process ws handler error: {exc!r}")
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            await unsubscribe(pid, queue)

    async def handle_autostart_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        return JSONResponse({"autostart": get_autostart_storage().list(profile)})

    async def handle_autostart_create(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        pid = (body.get("process_id") or "").strip()
        force = bool(body.get("force") or False)
        if not pid:
            return JSONResponse(
                {"error": "Missing parameter", "message": "process_id is required"},
                status_code=400,
            )

        info = _process_registry.get(pid)
        if info is None:
            return JSONResponse({"error": "Process not found"}, status_code=404)
        if profile and info.profile and info.profile != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        storage = get_autostart_storage()
        duplicate = storage.find_duplicate(profile, info.command)
        if duplicate and not force:
            return JSONResponse(
                {
                    "error": "duplicate",
                    "message": "A registration with the same command already exists.",
                    "existing": duplicate,
                },
                status_code=409,
            )

        row = storage.insert(
            profile=profile,
            command=info.command,
            working_dir=info.working_dir,
            is_pty=info.is_pty,
        )
        # Link the live process to the new registration so the UI sees the
        # star filled without waiting for the next boot. While linked, the
        # process opts out of the registry's TTL — registration is the
        # user's "keep this running indefinitely" signal.
        info.autostart_id = row["id"]
        info.expire_time = float("inf")
        publish_process_list_changed(profile)
        return JSONResponse(row)

    async def handle_autostart_delete(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        autostart_id = request.path_params["id"]
        storage = get_autostart_storage()
        existing = storage.get(autostart_id)
        if existing is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and existing.get("profile") and existing["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        storage.delete(autostart_id, profile)
        # If there's a live process linked to this registration, unlink it so
        # the star clears on the next refresh. The process keeps running, but
        # re-arm a fresh TTL window since it's no longer "indefinite by
        # registration" — normal cleanup applies again from now on.
        fresh_expire = time.monotonic() + (_DEFAULT_CLEANUP_TTL_HOURS * 3600)
        for info in _process_registry.values():
            if info.autostart_id == autostart_id:
                info.autostart_id = None
                info.expire_time = fresh_expire
        publish_process_list_changed(profile)
        return JSONResponse({"ok": True})

    async def handle_autostart_run(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        autostart_id = request.path_params["id"]
        storage = get_autostart_storage()
        row = storage.get(autostart_id)
        if row is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and row.get("profile") and row["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        process_id, error = await spawn_from_autostart(row)
        if error:
            storage.set_error(autostart_id, error)
            publish_process_list_changed(profile)
            return JSONResponse(
                {"error": "spawn_failed", "message": error}, status_code=400,
            )
        storage.clear_error(autostart_id)
        return JSONResponse({"process_id": process_id})

    async def handle_stream(request: Request) -> Any:
        """SSE endpoint pushing the live process list for the caller's profile.

        Replaces the old 5s ``GET /api/processes`` poll. On connect, the
        first frame is a ``snapshot`` containing the current
        ``list_processes(profile)`` result. Subsequent ``snapshot`` frames
        are emitted whenever a process spawns, exits, is stopped, or an
        autostart row mutates. A ``ready`` marker separates the initial
        snapshot from the live tail; a comment keepalive is emitted every
        15s so proxies don't drop the idle connection.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)

        bus = get_processes_stream_bus()
        queue = bus.subscribe(profile)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                yield _frame({
                    "type": "snapshot",
                    "data": {"processes": list_processes(profile)},
                })
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame({"type": "snapshot", "data": entry})
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
        Route("/api/processes", handle_list, methods=["GET"]),
        Route("/api/processes/stream", handle_stream, methods=["GET"]),
        Route("/api/processes/{pid}", handle_get, methods=["GET"]),
        Route("/api/processes/{pid}/stop", handle_stop, methods=["POST"]),
        Route("/api/processes/{pid}/stdin", handle_stdin, methods=["POST"]),
        Route("/api/processes/{pid}/resize", handle_resize, methods=["POST"]),
        WebSocketRoute("/api/processes/{pid}/ws", handle_ws),
        Route("/api/autostart-processes", handle_autostart_list, methods=["GET"]),
        Route("/api/autostart-processes", handle_autostart_create, methods=["POST"]),
        Route(
            "/api/autostart-processes/{id}",
            handle_autostart_delete, methods=["DELETE"],
        ),
        Route(
            "/api/autostart-processes/{id}/run",
            handle_autostart_run, methods=["POST"],
        ),
    ]
