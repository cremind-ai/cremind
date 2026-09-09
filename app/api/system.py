"""System-level admin endpoints exposed to the Developer page.

  POST /api/system/restart      — admin; asks the server to shut itself down
                                  gracefully, so the supervisor (Docker,
                                  Electron, the boot service, …) brings it
                                  back.
  GET  /api/system/environment  — admin; what kind of install this is
                                  (release channel, Docker/native/Kubernetes,
                                  VNC, paths), for the Developer page's
                                  Environment card and the config re-download.

The environment body is :func:`describe_runtime_environment` verbatim plus two
per-request fields, so the two nested blocks that description grew travel from
here to the Environment card, to ``cremind server environment`` and into the
exported config file: ``kubernetes`` (this pod's namespace, Helm release and
Deployment/Service, with the ``kubectl port-forward`` line that reconnects to
it) and ``vnc`` (how the desktop is reached, and the commands it takes to get
there). Both are admin-only on purpose - they name cluster objects and print
ready-to-run kubectl lines - which is why the unauthenticated tray descriptor
in :mod:`app.api.features` publishes only
``runtime_env.public_vnc_descriptor``'s four fields and no identity at all.

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
from app.config.runtime_env import describe_runtime_environment
from app.system.restart import DEFAULT_GRACE_S
from app.utils.logger import logger


# How long to let the 202 travel before the listener starts going away.
# Without it the client sees ECONNREFUSED with no warning.
_RESPONSE_WINDOW_S = 1.5


def schedule_system_restart() -> int:
    """Arm the detached watchdog and schedule a graceful server shutdown.

    TLS activation calls this after every browser handoff has been prepared,
    so a renderer crash between persistence and a second API call cannot leave
    a supervised native installation stuck on HTTP.
    """
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

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
        close_fds=True,
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
    return proc.pid


async def post_system_restart(request: Request) -> JSONResponse:
    """Spawn the watchdog, then ask ourselves to stop. Returns 202 or 500."""
    denied = require_admin(request)
    if denied is not None:
        return denied

    try:
        pid = schedule_system_restart()
    except OSError as e:
        return JSONResponse(
            {"error": f"Failed to spawn restart helper: {e}"},
            status_code=500,
        )

    # No in-flight guard: a second POST is harmless end-to-end. uvicorn's
    # ``handle_exit`` only re-sets ``should_exit`` for SIGTERM, a second
    # hard-exit timer changes nothing, and each watchdog holds its own handle
    # on this process.
    return JSONResponse(
        {"ok": True, "pid": pid, "status": "restarting"},
        status_code=202,
    )


async def get_system_environment(request: Request) -> JSONResponse:
    """Describe the install this server is running in (admin).

    Same gate as the restart above: the description names the system and
    install directories, the bind host and the public URL, which is deployment
    detail an ordinary profile has no business reading. The non-secret subset
    (install mode, deployment, release channel, VNC) is also published
    unauthenticated on ``/api/services/tray-capabilities`` for the Electron
    tray and ``cremind server capabilities``.

    ``deployment_custom_fields`` mirrors the four advanced fields the installer
    asks for on a ``custom`` deployment (install/catalog.toml) so the Developer
    page can re-render the Setup Wizard's config export long after setup, when
    the wizard's own answers are gone.

    Two blocks of the description are nested and deserve naming here because
    they are why this endpoint is worth calling on a pod. ``kubernetes`` is the
    identity the chart states (namespace, Helm release, Deployment/Service,
    Service port) with the ``kubectl port-forward`` command to reach it, or
    ``None`` anywhere else; ``vnc`` says how the desktop is reached
    (``direct`` / ``same_origin`` / ``port_forward``) and carries the
    port-forward commands that shape needs. The identity is deliberately
    admin-only: it names objects in someone's cluster, so
    ``/api/services/tray-capabilities`` - which answers with no token at all -
    gets only the four public VNC fields and never sees this block.

    The handler is free to add fields on top of the description because
    :func:`describe_runtime_environment` hands back a deep copy; mutating what
    it returns cannot reach the process-wide cache those nested blocks live in.

    ``effective_timezone`` is added here rather than in the shared description
    because it is a per-profile answer: :mod:`app.config.timezone` resolves the
    caller's own ``system.timezone`` row first, then the admin profile's
    inherited one, and only then the ``CREMIND_TIMEZONE`` boot default the
    description carries. The zone a schedule fires in is that resolved value —
    reporting the env var alone said "no timezone configured" on every install
    where someone had set one on the Config page.
    """
    denied = require_admin(request)
    if denied is not None:
        return denied

    from app.config.settings import BaseConfig
    from app.config.timezone import resolve_tz_name

    return JSONResponse({
        **describe_runtime_environment(),
        "effective_timezone": resolve_tz_name(
            getattr(request.user, "username", "") or None
        ),
        "deployment_custom_fields": {
            "listen_host": BaseConfig.HOST,
            "public_url": BaseConfig.APP_URL,
            # The raw env string, not BaseConfig's parsed list: the export
            # writes this back into a .env file verbatim, and the parsed form
            # defaults to ``["*"]`` where the operator wrote nothing at all.
            "allowed_origins": os.environ.get("CORS_ALLOWED_ORIGINS", ""),
            "wizard_preset": os.environ.get("SETUP_WIZARD_ENV", ""),
        },
    })


def get_system_routes() -> list[Route]:
    return [
        Route("/api/system/restart", post_system_restart, methods=["POST"]),
        Route("/api/system/environment", get_system_environment, methods=["GET"]),
    ]
