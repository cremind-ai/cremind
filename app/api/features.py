"""Feature install/status endpoints.

The Setup Wizard and the post-setup Settings page POST to
``/api/features/install`` to enable an optional feature. The endpoint
streams pip output as Server-Sent Events so a 30-second
``sentence-transformers`` install can render incremental progress in
the UI.

Both routes are pre-storage: the Setup Wizard runs before any DB exists
and needs to know which features will require an install before the user
clicks "Apply". Admin auth is enforced once setup is complete; before
that we mirror the wizard's deliberately-unauthenticated bootstrap
window.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.config.install_catalog import (
    apply_mode_rule_to_services,
    get_active_install_mode,
)
from app.features import installer
from app.features.installer import InstallEvent, InstallResult
from app.runtime import get_state
from app.services import get_capabilities_payload
from app.services.provisioner import docker_available
from app.utils.logger import logger


# ── status ────────────────────────────────────────────────────────────────

async def get_features(request: Request) -> JSONResponse:
    """Snapshot of every feature's install state.

    Returns ``{ feature_id: { installed, requires_restart_after_install,
    extras } }``. Unauthenticated until setup is complete so the wizard
    can render its "this will install …" hints before the admin token
    exists.
    """
    state = get_state()
    setup_complete = state.storage_ready and state.config_storage.is_setup_complete()
    if setup_complete:
        denied = require_admin(request)
        if denied is not None:
            return denied
    return JSONResponse(installer.feature_status())


# ── install (SSE) ─────────────────────────────────────────────────────────

async def post_install_features(request: Request) -> Any:
    """Install the listed features and stream pip output as SSE.

    Body: ``{"features": ["embedding.me5", "vectorstore.qdrant"]}``.
    Each pip stdout line is emitted as ``event: log``. When the install
    finishes, a final ``event: done`` carries the :class:`InstallResult`
    as JSON. Errors emit ``event: error`` with the failure message.
    """
    state = get_state()
    setup_complete = state.storage_ready and state.config_storage.is_setup_complete()
    if setup_complete:
        denied = require_admin(request)
        if denied is not None:
            return denied

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    raw_features = body.get("features") or []
    if not isinstance(raw_features, list) or not all(isinstance(k, str) for k in raw_features):
        return JSONResponse(
            {"error": "`features` must be a list of feature-id strings"},
            status_code=400,
        )

    feature_keys: list[str] = list(raw_features)

    # Pump events from the worker thread (where pip runs) into the async
    # SSE generator via a thread-safe queue. ``None`` is the sentinel that
    # tells the generator the install is finished.
    event_queue: queue.Queue[InstallEvent | None] = queue.Queue()
    result_holder: dict[str, InstallResult | Exception] = {}

    def _emit(event: InstallEvent) -> None:
        event_queue.put(event)

    def _worker() -> None:
        try:
            result_holder["result"] = installer.install_features(feature_keys, _emit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Feature install crashed unexpectedly")
            result_holder["error"] = exc
        finally:
            event_queue.put(None)

    threading.Thread(target=_worker, daemon=True, name="feature-installer").start()

    async def _stream():
        loop = asyncio.get_running_loop()
        while True:
            event = await loop.run_in_executor(None, event_queue.get)
            if event is None:
                # Worker is done. Surface the final result/error.
                if "error" in result_holder:
                    payload = {"error": str(result_holder["error"])}
                    yield _sse("done", payload, ok=False)
                else:
                    result = result_holder.get("result")
                    if result is None:
                        yield _sse("done", {"error": "install produced no result"}, ok=False)
                    else:
                        yield _sse("done", _result_payload(result), ok=not result.failed)
                return
            yield _sse(event.kind, _event_payload(event), ok=event.ok)

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse(event_name: str, data: dict, *, ok: bool = True) -> str:
    """Encode one Server-Sent Event frame.

    ``ok=False`` is reflected inside ``data`` rather than at the SSE
    protocol layer (SSE has no native error channel) so the client can
    branch on the JSON payload.
    """
    payload = dict(data)
    payload["ok"] = ok
    return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"


def _event_payload(event: InstallEvent) -> dict:
    return {"message": event.message, "meta": event.meta}


def _result_payload(result: InstallResult) -> dict:
    return {
        "restart_required": result.restart_required,
        "installed": result.installed,
        "failed": result.failed,
        "already_present": result.already_present,
        "error": result.error,
    }


# ── service deployment-mode capabilities ──────────────────────────────────


# Names of UI features the bundled SPA exposes — each entry corresponds
# to a top-level Vue Router segment under ``/:profile/<name>`` in
# ui/src/router/index.ts. The Electron app's tray / jumplist / dock
# builders read this list and only surface menu entries whose route the
# backend actually has; without the gate, a newer Electron pinned to an
# older backend wheel (the cross-version install case enabled by
# v0.1.9-test9's ``--version`` flag) would surface entries that land on
# the SPA's fallback page on click.
#
# Contract: when a new SPA page is added to the router, append its
# segment name here in the SAME commit. Both ship inside the same wheel
# so they move together by construction; the only failure mode is
# forgetting to update one of them, which is what this comment is here
# to prevent.
#
# Fallback rule on the Electron side: if the field is ABSENT from the
# response (older backend predating this protocol), the gated entries
# are hidden — not shown. The entries didn't exist before this protocol
# either, so a pre-protocol backend is exactly the case where the SPA
# can't service the click. Keeping older-pinned installs from drawing
# dead entries is the whole point of the gate.
UI_FEATURES: tuple[str, ...] = (
    "processes",
    "events",
    "channels",
)


async def get_service_capabilities(request: Request) -> JSONResponse:
    """Per-service deployment-mode descriptor for the Setup Wizard.

    Returns the static :mod:`app.services.manifest` snapshot plus a
    ``docker_available`` flag derived from runtime probing — the wizard
    masks the Docker radio option on installs where there's no compose
    file or docker socket on this host.

    Policy filter: ``install_catalog.toml`` defines a ``mode_rules``
    table that maps each install mode (Docker / Native / Custom) to
    the set of service deployment modes the wizard should show. The
    filter is applied server-side as well so advanced users can't
    submit a combination the install mode doesn't support. The active
    install mode is read from the ``INSTALL_MODE`` env var written by
    the installer; when unset, no filter is applied.

    Unauthenticated until setup is complete so the wizard can render
    mode pickers before the admin token exists.
    """
    state = get_state()
    setup_complete = state.storage_ready and state.config_storage.is_setup_complete()
    if setup_complete:
        denied = require_admin(request)
        if denied is not None:
            return denied

    docker_avail = docker_available()
    payload = get_capabilities_payload()
    install_mode = get_active_install_mode()
    apply_mode_rule_to_services(payload, install_mode)

    return JSONResponse({
        "services": payload,
        "docker_available": docker_avail,
        "install_mode": install_mode,
        # Tray/jumplist/dock gating list — see UI_FEATURES docstring above.
        "ui_features": list(UI_FEATURES),
    })


async def get_tray_capabilities(_request: Request) -> JSONResponse:
    """Public tray/jumplist/dock gating descriptor for the Electron main
    process.

    Returns just the two fields the Electron main process needs to gate
    its menu entries: the active install mode (drives the "Open VNC
    Desktop" entry, shown on Docker installs only) and the list of UI
    feature names the bundled SPA exposes (drives Process Manager /
    Events / Channels).

    Deliberately unauthenticated. The Electron main process can't share
    the renderer's session cookies, so the admin-gated
    ``/api/services/capabilities`` endpoint stops being reachable as
    soon as setup completes — that broke the gate on every post-setup
    launch. The data returned here is route-name + install-mode
    metadata with no security value; the richer capabilities endpoint
    stays admin-gated for the Setup Wizard's deeper payload.
    """
    return JSONResponse({
        "install_mode": get_active_install_mode(),
        "ui_features": list(UI_FEATURES),
    })


# ── route registration ────────────────────────────────────────────────────

def get_features_routes() -> list[Route]:
    return [
        Route("/api/features", get_features, methods=["GET"]),
        Route("/api/features/install", post_install_features, methods=["POST"]),
        Route("/api/services/capabilities", get_service_capabilities, methods=["GET"]),
        Route("/api/services/tray-capabilities", get_tray_capabilities, methods=["GET"]),
    ]
