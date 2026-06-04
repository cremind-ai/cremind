"""Upgrade-availability + apply endpoints for the in-app updater UI.

Five endpoints:

  GET  /api/upgrade/check    — unauth; "is a newer version available?"
  GET  /api/upgrade/versions — unauth; test-channel installable releases (the
                               version picker); empty list on production / dev.
  POST /api/upgrade/apply    — auth; spawns a detached upgrade runner. Optional
                               ``{"target_version": …}`` body installs a specific
                               test release (ignored off the test channel).
  GET  /api/upgrade/status   — auth; current phase + log tail.
  GET  /api/upgrade/stream   — auth; SSE wrapping ``status`` for live UI.

``/apply`` doesn't run the upgrade in-process. It spawns
``python -m app.upgrade.detached`` as a sibling subprocess and returns
202 immediately — that subprocess does install + migrate + restart, and
writes progress to ``~/.cremind/.upgrade.status.json`` along the way.
The HTTP server can't safely upgrade itself in-process because the
runner replaces its own wheel files on disk and then expects the
parent to be restarted; doing that inside a request handler would
kill the connection mid-response and leave the renderer guessing.

The status file is the durable handoff: when the backend restarts on
the new wheel, the new process re-reads the file and surfaces the
terminal result to whichever frontend is still polling.

``/apply`` is auth-gated. The other read-only endpoints intentionally
mirror ``/check`` and remain open: ``GET /api/upgrade/check`` is
already public because the version is public, and ``/status`` /
``/stream`` only ever expose data the user already has access to via
the renderer. (No information leak beyond "an upgrade is in progress.")
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.__version__ import __version__ as CURRENT_VERSION
from app.api._auth import require_auth


async def get_upgrade_check(_request: Request) -> JSONResponse:
    # Imports are lazy because the upgrade module pulls in urllib and
    # the manifest helpers — fine at runtime, but unnecessary work
    # during the API-route registration pass.
    try:
        from app.upgrade.channel import get_channel
        from app.upgrade.manifest import fetch_latest, is_at_or_above, is_newer
    except ImportError:
        return JSONResponse(
            {
                "current": CURRENT_VERSION,
                "status": "unavailable",
                "reason": "Upgrade module not installed.",
            }
        )

    channel = get_channel()
    try:
        release = fetch_latest(channel=channel)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {
                "current": CURRENT_VERSION,
                "channel": channel,
                "status": "unreachable",
                "reason": str(e),
            }
        )

    if not is_newer(release.version, CURRENT_VERSION):
        return JSONResponse(
            {
                "current": CURRENT_VERSION,
                "latest": release.version,
                "channel": channel,
                "status": "up_to_date",
                "release_url": release.html_url,
            }
        )

    if not is_at_or_above(CURRENT_VERSION, release.min_supported_upgrade_from):
        # The new release refuses to migrate from us — surface it so
        # the UI can route the user to the legacy-export instructions
        # instead of pretending an in-place upgrade will work.
        return JSONResponse(
            {
                "current": CURRENT_VERSION,
                "latest": release.version,
                "channel": channel,
                "status": "too_old",
                "min_supported_upgrade_from": release.min_supported_upgrade_from,
                "release_url": release.html_url,
            }
        )

    return JSONResponse(
        {
            "current": CURRENT_VERSION,
            "latest": release.version,
            "channel": channel,
            "status": "available",
            "release_url": release.html_url,
            "release_notes": release.body,
        }
    )


async def get_upgrade_versions(_request: Request) -> JSONResponse:
    """List installable releases for the version picker.

    Test channel only — it returns the ranked list of RC dev releases so a
    tester can switch to a specific PR's RC. Production and dev always get an
    empty list (production upgrades to the latest final; dev runs the working
    copy), so the picker never appears there.
    """
    try:
        from app.upgrade.channel import get_channel
        from app.upgrade.manifest import list_releases
    except ImportError:
        return JSONResponse(
            {"current": CURRENT_VERSION, "channel": "unknown", "versions": []}
        )

    channel = get_channel()
    if channel != "test":
        return JSONResponse(
            {"current": CURRENT_VERSION, "channel": channel, "versions": []}
        )

    try:
        releases = list_releases(channel="test")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {
                "current": CURRENT_VERSION,
                "channel": channel,
                "versions": [],
                "reason": str(e),
            }
        )

    return JSONResponse(
        {
            "current": CURRENT_VERSION,
            "channel": channel,
            "versions": [
                {
                    "version": r.version,
                    "tag_name": r.tag_name,
                    "name": r.name,
                    "published_at": r.published_at,
                    "html_url": r.html_url,
                }
                for r in releases
            ],
        }
    )


async def post_upgrade_apply(request: Request) -> JSONResponse:
    """Spawn the detached upgrade runner. Returns 202 or 409."""
    denied = require_auth(request)
    if denied is not None:
        return denied

    try:
        from app.upgrade import status
    except ImportError:
        return JSONResponse(
            {"error": "Upgrade module not installed."},
            status_code=503,
        )

    if status.is_running():
        return JSONResponse(
            {
                "error": "An upgrade is already running.",
                "status_url": "/api/upgrade/status",
            },
            status_code=409,
        )

    # Optional ``{"target_version": "X.Y.ZrcN.devM"}`` from the test-channel
    # version picker. Honored ONLY on the test channel — on production / dev
    # we ignore it and upgrade to the latest, exactly as before, so the
    # picker can never affect those channels even if a body is sent.
    target_version: str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            tv = body.get("target_version")
            if isinstance(tv, str) and tv.strip():
                target_version = tv.strip()
    except Exception:  # noqa: BLE001 — no/!json body is the common case
        target_version = None

    if target_version is not None:
        from app.upgrade.channel import get_channel

        if get_channel() != "test":
            target_version = None

    # Resolve the cremind executable path the same way the runner does
    # for its own subprocesses — sys.executable + ``-m app.upgrade.detached``
    # bypasses any PATH ambiguity from console-script shims that may
    # not exist on a freshly-installed venv.
    python = sys.executable
    cmd = [python, "-m", "app.upgrade.detached", "--parent-pid", str(os.getpid())]
    if target_version is not None:
        cmd += ["--target-version", target_version]

    log_path = _spawn_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The detached child writes structured progress to the status file;
    # raw stdout/stderr go here as a debugging breadcrumb. Append so a
    # second upgrade run keeps history.
    log_handle = log_path.open("a", encoding="utf-8", errors="replace")

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        # Detach so killing this process doesn't take the upgrade with it.
        # CREATE_NEW_PROCESS_GROUP also stops Ctrl-C in the parent shell
        # from propagating into the upgrade.
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX: a new session means the child survives parent SIGTERM
        # (which the runner triggers itself at the end of a successful
        # upgrade). Without this the kernel kills our own child when we
        # die.
        start_new_session = True

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
            close_fds=True,
        )
    except OSError as e:
        log_handle.close()
        return JSONResponse(
            {"error": f"Failed to spawn upgrade runner: {e}"},
            status_code=500,
        )
    finally:
        # We've handed the fd to the child; closing it here is fine on
        # POSIX (Popen dup'd it), and on Windows the inheritance is
        # already set up by the time Popen returns.
        try:
            log_handle.close()
        except OSError:
            pass

    return JSONResponse(
        {
            "ok": True,
            "pid": proc.pid,
            "status_url": "/api/upgrade/status",
            "stream_url": "/api/upgrade/stream",
        },
        status_code=202,
    )


async def get_upgrade_status(request: Request) -> JSONResponse:
    denied = require_auth(request)
    if denied is not None:
        return denied
    try:
        from app.upgrade import status
    except ImportError:
        return JSONResponse({"phase": "idle", "ok": True, "log_tail": []})
    return JSONResponse(status.read())


async def get_upgrade_stream(request: Request):
    """SSE stream over the same status file ``GET /status`` returns.

    The stream stays open as long as the upgrade is running, emitting
    one ``status`` event per change. When the backend restarts in the
    middle of an upgrade, the connection drops; the renderer is
    expected to fall back to polling ``/status`` until it can
    reconnect. We don't try to hide the gap — it's part of how the
    architecture works.
    """
    denied = require_auth(request)
    if denied is not None:
        return denied

    from starlette.responses import StreamingResponse

    try:
        from app.upgrade import status
    except ImportError:

        async def _empty():
            yield b'event: status\ndata: {"phase":"idle"}\n\n'

        return StreamingResponse(_empty(), media_type="text/event-stream")

    async def _events():
        import json

        last_seen: tuple | None = None
        # 500 ms cadence is brisk enough that the user perceives the log
        # as live without hammering the disk. The upgrade runner emits
        # at human pace anyway (one event per pip install line).
        while True:
            if await request.is_disconnected():
                return
            state = status.read()
            # Fingerprint = (phase, len(log_tail)). Cheap, no diff needed.
            fp = (state.get("phase"), len(state.get("log_tail") or []))
            if fp != last_seen:
                last_seen = fp
                yield (b"event: status\ndata: " + json.dumps(state).encode("utf-8") + b"\n\n")
            if state.get("phase") in ("done", "failed"):
                # One final emit was just sent above; close the stream
                # so the renderer falls back to one-shot /status reads.
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering — without this Nginx/Cloud Run
            # batch SSE events and the user sees the log update in
            # 30-second jumps.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _spawn_log_path() -> Path:
    """Where the detached subprocess's raw stdout/stderr lands."""
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / "upgrade-detached.log"


def get_upgrade_routes() -> list[Route]:
    return [
        Route("/api/upgrade/check", get_upgrade_check, methods=["GET"]),
        Route("/api/upgrade/versions", get_upgrade_versions, methods=["GET"]),
        Route("/api/upgrade/apply", post_upgrade_apply, methods=["POST"]),
        Route("/api/upgrade/status", get_upgrade_status, methods=["GET"]),
        Route("/api/upgrade/stream", get_upgrade_stream, methods=["GET"]),
    ]
