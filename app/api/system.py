"""System-level admin endpoints exposed to the Developer page.

  POST /api/system/restart  — admin; asks the server to shut itself down
                              gracefully, so the supervisor (Docker,
                              Electron, the boot service, …) brings it back.

The server can't stop in-process the instant the request arrives: the
connection would drop mid-response and the client would see
ECONNREFUSED with no warning. So the handler returns 202 and schedules
the shutdown a beat later, through
:func:`app.server.request_graceful_shutdown` — the same path an OS
signal takes, which drains connections, stops channel adapters and
their node sidecars, tree-kills managed processes and releases their
lock files before the process exits.

A detached sibling (:mod:`app.system.restart`) is spawned first, as a
watchdog: it waits for this process to exit on its own and hard-kills it
only if the shutdown wedged. That inversion is the point — the previous
design had the helper do the killing, and on Windows ``os.kill(pid,
SIGTERM)`` is ``TerminateProcess``, so every restart skipped the cleanup
above and orphaned whatever the server had spawned.
"""

from __future__ import annotations

import os
import subprocess
import sys

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.system.restart import DEFAULT_GRACE_S
from app.utils.logger import logger


# How long to let the 202 travel before the listener starts going away.
# Without it the client sees ECONNREFUSED with no warning.
_RESPONSE_WINDOW_S = 1.5


async def post_system_restart(request: Request) -> JSONResponse:
    """Spawn the watchdog, then ask ourselves to stop. Returns 202 or 500."""
    denied = require_admin(request)
    if denied is not None:
        return denied

    # Same invocation pattern as ``/api/upgrade/apply`` — ``sys.executable``
    # + ``-m app.system.restart`` avoids any PATH ambiguity from console
    # script shims that may not exist on a fresh install.
    python = sys.executable
    cmd = [
        python,
        "-m",
        "app.system.restart",
        "--parent-pid",
        str(os.getpid()),
        "--grace",
        str(DEFAULT_GRACE_S),
    ]

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        # Detach so killing this process doesn't take the helper with it.
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX: new session means the child survives the shutdown it is
        # about to supervise. Without this the kernel kills our own child
        # when we die.
        start_new_session = True

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=start_new_session,
            close_fds=True,
        )
    except OSError as e:
        return JSONResponse(
            {"error": f"Failed to spawn restart helper: {e}"},
            status_code=500,
        )

    # Only now, with the watchdog running: if the spawn had failed after the
    # shutdown was scheduled, a wedged shutdown would have had nothing left to
    # rescue it. Imported here because ``app.server`` imports this module at
    # boot — a top-level import would be a cycle.
    from app import server as _server

    if not _server.request_graceful_shutdown(_RESPONSE_WINDOW_S):
        logger.warning(
            "Restart requested with no serving loop to stop; the watchdog "
            f"will stop this process at the {DEFAULT_GRACE_S}s deadline."
        )

    # No in-flight guard: a second POST is harmless end-to-end. uvicorn's
    # ``handle_exit`` only re-sets ``should_exit`` for SIGTERM, a second
    # hard-exit timer changes nothing, and each watchdog holds its own handle
    # on this process.
    return JSONResponse(
        {"ok": True, "pid": proc.pid, "status": "restarting"},
        status_code=202,
    )


def get_system_routes() -> list[Route]:
    return [
        Route("/api/system/restart", post_system_restart, methods=["POST"]),
    ]
