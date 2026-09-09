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
from app.config import runtime_env
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


def get_image_flavor() -> str | None:
    """The Docker image flavor this container was built as, or ``None``.

    The implementation moved to :mod:`app.config.runtime_env`, which owns every
    other install fact too; this stays as the name the Electron-facing
    endpoints below (and their tests) already call. Uncached on both sides on
    purpose — a caller that sets ``CREMIND_IMAGE_FLAVOR`` and asks again must
    get the new answer.
    """
    return runtime_env.get_image_flavor()


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
        # Docker image flavor (desktop / basic / None) — see get_image_flavor.
        "image_flavor": get_image_flavor(),
        # Tray/jumplist/dock gating list — see UI_FEATURES docstring above.
        "ui_features": list(UI_FEATURES),
        # What TLS this server is serving, and what it will serve next. The
        # wizard needs this BEFORE the admin token exists (to decide whether to
        # show the "trust the CA" step), which is exactly what this endpoint's
        # open-until-setup-completes gate above provides.
        "tls": _tls_capabilities(request),
    })


def _tls_capabilities(request: Request) -> dict:
    """The ``tls`` block of the capabilities payload.

    ``pending_https`` carries the whole decision for the UI: it is true only
    when this server really will switch to HTTPS on its next boot, and false
    wherever TLS can never happen (``CREMIND_UI_PORT=0``) — so the
    wizard gating on it inherits those exclusions without sniffing the
    environment itself.
    """
    from app.api.tls import local_trust_capabilities
    from app.config.settings import BaseConfig
    from app.config.tls_mode import current_tls_facts, https_origin_from_app_url
    from app.config.tls_transition import certificate_info

    facts = current_tls_facts()
    edge_https = getattr(getattr(request, "url", None), "scheme", "http") == "https" and not facts.serving_https
    https_url = (
        https_origin_from_app_url(BaseConfig.APP_URL)
        if (facts.serving_https or edge_https or facts.pending_https)
        else None
    )
    return {
        "mode": "custom" if BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE else facts.mode,
        "serving_https": facts.serving_https or edge_https,
        "pending_https": facts.pending_https,
        "ca_sha256": certificate_info(external=edge_https)["ca_sha256"],
        "https_url": https_url or None,
        "restart_supported": facts.restart_supported,
        # Whether THIS server can put the CA into THIS device's trust store —
        # true only on a native install answering its own machine's browser
        # (the request's peer is loopback). Drives the wizard's one-click
        # "Trust it on this device" button; everywhere else the wizard shows
        # the manual per-OS commands.
        "local_trust": local_trust_capabilities(request),
    }


async def get_tray_capabilities(_request: Request) -> JSONResponse:
    """Public tray/jumplist/dock gating descriptor for the Electron main
    process.

    Returns the fields the Electron main process needs to gate its menu
    entries: the ``vnc`` descriptor that drives the "Open VNC Desktop" entry,
    and the list of UI feature names the bundled SPA exposes (drives Process
    Manager / Events / Channels). It also carries the handful of install facts
    that need no token - deployment, release channel, whether there is a VNC
    desktop, whether this is a container - so ``cremind server capabilities``
    can describe a server it has no admin token for.

    That entry is gated on ``vnc.access`` now, which is what makes it work on
    Kubernetes: the old gate was "Docker install whose image is the desktop
    flavor", so a pod never offered the desktop at all, and the shell that did
    open one aimed at ``localhost:6080`` where nothing listens until a tunnel
    exists. ``install_mode`` and ``image_flavor`` stay in the payload because
    an older Electron shell reads only those two and still has to run against
    a new backend.

    What travels here is only the public subset (see
    ``runtime_env.public_vnc_descriptor``): enough to decide whether the menu
    entry belongs and what it would open. This endpoint answers before anyone
    has signed in, so the namespace, the Service name, the ready-to-run
    ``kubectl`` commands and the composed URL stay on the admin-gated
    ``/api/system/environment`` and ``/api/config/install-secrets``.

    Deliberately unauthenticated. The Electron main process can't share
    the renderer's session cookies, so the admin-gated
    ``/api/services/capabilities`` endpoint stops being reachable as
    soon as setup completes — that broke the gate on every post-setup
    launch. The data returned here is route-name + install-mode
    metadata with no security value; the richer capabilities endpoint
    stays admin-gated for the Setup Wizard's deeper payload.
    """
    env = runtime_env.describe_runtime_environment()

    return JSONResponse({
        # The same install mode ``/api/system/environment`` reports, so the two
        # commands that read them (``server capabilities`` and ``server
        # environment``) cannot disagree about the same machine. It differs
        # from ``get_active_install_mode()`` — which the Setup Wizard's own
        # endpoint above keeps, because there ``None`` means "apply no
        # service-mode filter" — only on an install whose ``.env`` predates the
        # INSTALL_MODE key: a pre-flavor Docker image, which this reports as
        # the ``docker`` it is instead of ``null``. That also lets the Electron
        # client offer "Open VNC Desktop" on those images, which is right —
        # every pre-flavor image is a desktop image.
        "install_mode": env["install_mode"],
        # desktop / basic / None. None (native or a pre-flavor image) is
        # treated as desktop for Docker installs by the Electron client.
        "image_flavor": get_image_flavor(),
        "ui_features": list(UI_FEATURES),
        # Whether something restarts this process when it exits — the broad
        # question, from the shared description: a container's restart policy
        # or the kubelet counts, so does Electron, and so does the boot service
        # (`cremind boot enable`) that install_mode alone cannot reveal. Both
        # consumers ask it that way (the Developer page's restart dialog and
        # `cremind server restart` pick their caveat from it), and reading only
        # CREMIND_SUPERVISED here — which nothing in install/ or helm/ sets for
        # a container — told a Docker user their backend would stay down while
        # /api/system/environment said the opposite about the same process.
        "supervised": env["supervised"],
        # Non-secret install facts from the shared description. Nothing here
        # names a path, a host or a credential — the admin-gated
        # /api/system/environment carries those.
        "deployment": env["deployment"],
        "release_channel": env["release_channel"],
        "vnc_enabled": env["vnc_enabled"],
        "container": env["container"],
        # {enabled, access, novnc_path, novnc_port} and nothing else: the
        # single place that draws the line between what an unauthenticated
        # caller may know about the desktop and what stays admin-only is
        # public_vnc_descriptor, so a field added to the full descriptor later
        # cannot leak out through here by accident.
        "vnc": runtime_env.public_vnc_descriptor(env["vnc"]),
    })


# ── route registration ────────────────────────────────────────────────────

def get_features_routes() -> list[Route]:
    return [
        Route("/api/features", get_features, methods=["GET"]),
        Route("/api/features/install", post_install_features, methods=["POST"]),
        Route("/api/services/capabilities", get_service_capabilities, methods=["GET"]),
        Route("/api/services/tray-capabilities", get_tray_capabilities, methods=["GET"]),
    ]
