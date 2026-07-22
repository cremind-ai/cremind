"""User-created interactive terminals.

A side feature independent of the agent: the UI's "New terminal" button
spawns a bare interactive OS shell under a PTY and streams it to xterm.js
over a WebSocket. These sessions are deliberately **not** part of the
``exec_shell`` Process Manager — they live in a private ``_terminal_registry``
so the exec_shell sweeps (``cancel_processes_by_task`` on agent-run end,
the TTL cleanup, ``stop_processes_for_profile`` on reset) can never touch
them, and they never appear in ``GET /api/processes``.

The WebSocket protocol is intentionally frame-compatible with the Process
Manager one (see ``app/api/processes.py``) so the frontend ``TerminalSession``
component reuses the same message handling.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

# Reuse the Process Manager's auth helpers (same JWT / profile model). Importing
# these privates cross-module is the established pattern (processes.py itself
# imports exec_shell privates).
from app.api.processes import (
    _decode_ws_token,
    _profile_from_request,
    _require_auth,
)
from app.config.settings import get_user_working_directory
from app.config.system_vars import build_system_env
from app.tools.builtin.exec_shell_pty import PtyProcess, spawn_interactive_shell_pty
from app.utils.logger import logger

# Late-joiner scrollback replayed on (re)connect. Chars, not bytes — a decoded
# string budget, matching exec_shell's ring-buffer accounting.
_RING_MAX_BYTES = 256 * 1024
# Per-subscriber backpressure: a stuck UI tab is evicted rather than stalling
# the pump.
_QUEUE_MAX = 256
# One user shouldn't be able to fork-bomb the server with terminals.
_MAX_TERMINALS_PER_PROFILE = 10
# Reap sessions that have had zero WS subscribers for this long (e.g. the tab
# was closed by a crash, or spawned but never attached). Lazy — checked on
# every create/list, no background timer.
_DETACHED_REAP_SECONDS = 24 * 3600


@dataclass
class TerminalInfo:
    terminal_id: str
    process: PtyProcess
    profile: str
    shell: str
    title: str
    working_dir: str
    created_at: float  # wall clock (Unix seconds)
    exit_code: Optional[int] = None
    ring: Deque[str] = field(default_factory=deque)
    ring_bytes: int = 0
    subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pump_task: Optional[asyncio.Task] = None
    # Monotonic timestamp of when subscribers last dropped to zero; None while
    # at least one WS is attached. Drives the detached-reap sweep.
    detached_since: Optional[float] = None


# Private registry — never the exec_shell ``_process_registry``.
_terminal_registry: Dict[str, TerminalInfo] = {}
# Per-profile monotonic counter for "Terminal N" titles (server-side so the
# numbering survives frontend reloads and never duplicates).
_title_counters: Dict[str, int] = {}


# ---------------------------------------------------------------------------
# Fan-out (mirrors exec_shell._broadcast_chunks, but local + stdout-only).
# ---------------------------------------------------------------------------

def _fanout_locked(info: TerminalInfo, message: Dict[str, Any]) -> None:
    """Push ``message`` to every subscriber. Caller must hold ``info.lock``.

    Evicts (and notifies with an ``overflow`` sentinel) any queue that has
    filled up, so one stalled tab can't back up the pump.
    """
    evicted: List["asyncio.Queue[Dict[str, Any]]"] = []
    for queue in info.subscribers:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            evicted.append(queue)
    for queue in evicted:
        info.subscribers.discard(queue)
        try:
            queue.put_nowait({"type": "overflow"})
        except asyncio.QueueFull:
            pass


async def _broadcast(info: TerminalInfo, message: Dict[str, Any]) -> None:
    async with info.lock:
        _fanout_locked(info, message)


async def _subscribe(
    info: TerminalInfo,
) -> "tuple[asyncio.Queue[Dict[str, Any]], List[str]]":
    """Atomically snapshot the ring buffer and register a live subscriber."""
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_QUEUE_MAX)
    async with info.lock:
        snapshot = list(info.ring)
        info.subscribers.add(queue)
        info.detached_since = None
    return queue, snapshot


async def _unsubscribe(
    info: TerminalInfo, queue: "asyncio.Queue[Dict[str, Any]]",
) -> None:
    async with info.lock:
        info.subscribers.discard(queue)
        if not info.subscribers:
            info.detached_since = time.monotonic()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _pump_output(info: TerminalInfo) -> None:
    """Read the PTY's merged output and fan it out until the shell exits."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            data = await info.process.stdout.read(4096)
            if not data:
                break
            text = decoder.decode(data)
            if not text:
                continue
            async with info.lock:
                info.ring.append(text)
                info.ring_bytes += len(text)
                while info.ring_bytes > _RING_MAX_BYTES and info.ring:
                    info.ring_bytes -= len(info.ring.popleft())
                _fanout_locked(info, {"type": "stdout", "data": text})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"terminal pump {info.terminal_id} read error: {exc!r}")
    finally:
        tail = decoder.decode(b"", final=True)
        if tail:
            await _broadcast(info, {"type": "stdout", "data": tail})
        try:
            info.exit_code = await info.process.wait()
        except Exception:  # noqa: BLE001
            info.exit_code = None
        await _broadcast(info, {
            "type": "status",
            "data": {
                "process_id": info.terminal_id,
                "command": info.title,
                "working_dir": info.working_dir,
                "is_pty": True,
                "status": "exited",
                "exit_code": info.exit_code,
            },
        })
        # Internal sentinel: tells attached WS handlers to close cleanly (1000).
        await _broadcast(info, {"type": "__closed"})
        try:
            info.process.close_master()
        except Exception:  # noqa: BLE001
            pass
        _terminal_registry.pop(info.terminal_id, None)


async def _terminate(info: TerminalInfo) -> None:
    """Terminate a terminal's shell (idempotent). The pump's finally does the
    ring/status/close_master/pop cleanup once the process actually dies."""
    try:
        info.process.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.wait_for(info.process.wait(), timeout=1.5)
    except Exception:  # noqa: BLE001 (TimeoutError or backend-specific)
        try:
            info.process.kill()
        except Exception:  # noqa: BLE001
            pass


def _reap_stale() -> None:
    """Schedule termination of terminals detached longer than the reap window."""
    now = time.monotonic()
    for info in list(_terminal_registry.values()):
        if (
            info.detached_since is not None
            and (now - info.detached_since) > _DETACHED_REAP_SECONDS
        ):
            asyncio.create_task(_terminate(info))


async def close_all_terminals() -> None:
    """Terminate every live terminal. Called from the server shutdown hook.

    In-memory only — PTY children also die with the server, so this is a
    best-effort graceful path, bounded so it can't stall shutdown.
    """
    infos = list(_terminal_registry.values())
    for info in infos:
        try:
            info.process.terminate()
        except Exception:  # noqa: BLE001
            pass
    for info in infos:
        try:
            await asyncio.wait_for(info.process.wait(), timeout=1.0)
        except Exception:  # noqa: BLE001
            try:
                info.process.kill()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def get_terminal_routes() -> list:
    """Collect the user-terminal HTTP + WebSocket routes."""

    async def handle_create(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        _reap_stale()

        live = [i for i in _terminal_registry.values() if i.profile == profile]
        if len(live) >= _MAX_TERMINALS_PER_PROFILE:
            return JSONResponse(
                {"error": f"Too many open terminals (max {_MAX_TERMINALS_PER_PROFILE})."},
                status_code=409,
            )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        cwd = body.get("cwd") if isinstance(body, dict) else None
        if not (isinstance(cwd, str) and cwd and os.path.isdir(cwd)):
            cwd = get_user_working_directory()
        try:
            cols = int(body.get("cols") or 80)
            rows = int(body.get("rows") or 24)
        except (TypeError, ValueError):
            cols, rows = 80, 24

        extra_env = build_system_env(profile)
        try:
            proc, shell = await spawn_interactive_shell_pty(
                cwd, cols=cols, rows=rows, extra_env=extra_env,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"terminal spawn failed (profile={profile!r}): {exc!r}")
            return JSONResponse(
                {"error": f"Failed to spawn terminal: {exc}"}, status_code=500,
            )

        _title_counters[profile] = _title_counters.get(profile, 0) + 1
        title = f"Terminal {_title_counters[profile]}"
        # ``term-`` prefix guarantees the id can never collide with an
        # exec_shell 8-hex pid in the UI's shared tab keying.
        tid = "term-" + uuid4().hex[:12]
        info = TerminalInfo(
            terminal_id=tid, process=proc, profile=profile, shell=shell,
            title=title, working_dir=cwd, created_at=time.time(),
            detached_since=time.monotonic(),  # no subscribers until the WS opens
        )
        _terminal_registry[tid] = info
        info.pump_task = asyncio.create_task(_pump_output(info))
        return JSONResponse({
            "terminal_id": tid,
            "title": title,
            "shell": shell,
            "working_dir": cwd,
            "created_at": info.created_at,
        }, status_code=201)

    async def handle_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        _reap_stale()
        terminals = [
            {
                "terminal_id": i.terminal_id,
                "title": i.title,
                "shell": i.shell,
                "working_dir": i.working_dir,
                "created_at": i.created_at,
                "status": "running",
            }
            for i in _terminal_registry.values()
            if i.profile == profile and i.exit_code is None
        ]
        return JSONResponse({"terminals": terminals})

    async def handle_close(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        tid = request.path_params["tid"]
        info = _terminal_registry.get(tid)
        if info is None:
            return JSONResponse({"error": "Terminal not found"}, status_code=404)
        if profile and info.profile and info.profile != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        await _terminate(info)
        return JSONResponse({"ok": True})

    async def handle_ws(websocket: WebSocket) -> None:
        # Token arrives as the ['bearer', <token>] subprotocol (browsers can't
        # set Authorization on the WebSocket constructor). Mirrors processes.py.
        subprotocols = list(websocket.scope.get("subprotocols") or [])
        token: Optional[str] = None
        if len(subprotocols) >= 2 and subprotocols[0] == "bearer":
            token = subprotocols[1]
        tid = websocket.path_params.get("tid") or ""

        payload = _decode_ws_token(token or "")
        if payload is None:
            logger.warning(
                f"terminal ws rejected (tid={tid}): missing or invalid auth token"
            )
            await websocket.close(code=1008)
            return

        profile = payload.get("profile") or payload.get("sub") or ""

        info = _terminal_registry.get(tid)
        if info is None:
            logger.info(f"terminal ws rejected: unknown terminal id {tid!r}")
            await websocket.close(code=1008)
            return
        if profile and info.profile and info.profile != profile:
            logger.warning(
                f"terminal ws rejected (tid={tid}): profile {profile!r} may not "
                f"access a terminal owned by {info.profile!r}"
            )
            await websocket.close(code=1008)
            return

        queue, snapshot = await _subscribe(info)
        await websocket.accept(subprotocol="bearer")

        try:
            await websocket.send_json({
                "type": "snapshot",
                "chunks": [{"type": "stdout", "data": chunk} for chunk in snapshot],
            })
            await websocket.send_json({
                "type": "status",
                "data": {
                    "process_id": info.terminal_id,
                    "command": info.title,
                    "working_dir": info.working_dir,
                    "is_pty": True,
                    "status": "exited" if info.exit_code is not None else "running",
                    "exit_code": info.exit_code,
                },
            })

            async def pump_to_client() -> None:
                while True:
                    message = await queue.get()
                    mtype = message.get("type")
                    if mtype == "__closed":
                        await websocket.close(code=1000)
                        return
                    if mtype == "overflow":
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
                    mtype = message.get("type")
                    if mtype == "stdin":
                        data = message.get("data")
                        if data:
                            try:
                                info.process.stdin.write(data)
                                await info.process.stdin.drain()
                            except Exception:  # noqa: BLE001
                                pass
                    elif mtype == "resize":
                        try:
                            cols = int(message.get("cols"))
                            rows = int(message.get("rows"))
                        except (TypeError, ValueError):
                            continue
                        try:
                            info.process.resize(cols, rows)
                        except Exception:  # noqa: BLE001
                            pass
                    elif mtype == "ping":
                        pass
                    # Unknown types are silently dropped.

            producer = asyncio.create_task(pump_to_client())
            consumer = asyncio.create_task(pump_from_client())
            done, pending = await asyncio.wait(
                {producer, consumer}, return_when=asyncio.FIRST_COMPLETED,
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
                    logger.warning(f"terminal ws task (tid={tid}) ended with {exc!r}")
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"terminal ws handler error (tid={tid}): {exc!r}")
            try:
                await websocket.close(code=1011)
            except Exception:  # noqa: BLE001
                pass
        finally:
            await _unsubscribe(info, queue)

    return [
        Route("/api/terminals", handle_create, methods=["POST"]),
        Route("/api/terminals", handle_list, methods=["GET"]),
        Route("/api/terminals/{tid}/close", handle_close, methods=["POST"]),
        WebSocketRoute("/api/terminals/{tid}/ws", handle_ws),
    ]
