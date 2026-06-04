"""System-level admin endpoints exposed to the Developer page.

  POST /api/system/restart  — admin; spawns a detached helper that
                              SIGTERMs the backend after the response
                              has been sent, so the supervisor
                              (Docker, Electron, …) can bring it back.

The HTTP server can't kill itself in-process: the connection would
drop mid-response and the client would see ECONNREFUSED with no
warning. So this mirrors the upgrade flow's approach — spawn a
sibling detached subprocess (see :mod:`app.system.restart`) and
return 202 immediately. The subprocess waits ~1.5 s then sends
SIGTERM to ``os.getpid()`` of the API process.
"""

from __future__ import annotations

import os
import subprocess
import sys

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_admin


async def post_system_restart(request: Request) -> JSONResponse:
    """Spawn the detached restart helper. Returns 202 or 500."""
    denied = require_admin(request)
    if denied is not None:
        return denied

    # Same invocation pattern as ``/api/upgrade/apply`` — ``sys.executable
    # + ``-m app.system.restart`` avoids any PATH ambiguity from console
    # script shims that may not exist on a fresh install.
    python = sys.executable
    cmd = [python, "-m", "app.system.restart", "--parent-pid", str(os.getpid())]

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        # Detach so killing this process doesn't take the helper with it.
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX: new session means the child survives the SIGTERM it's
        # about to send us. Without this the kernel kills our own child
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

    return JSONResponse(
        {"ok": True, "pid": proc.pid, "status": "restarting"},
        status_code=202,
    )


def get_system_routes() -> list[Route]:
    return [
        Route("/api/system/restart", post_system_restart, methods=["POST"]),
    ]
