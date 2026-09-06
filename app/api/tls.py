"""Local CA download and server-side trust.

``CREMIND_SSL=auto`` and ``CREMIND_SSL=after-setup`` both sign the server's
certificate with a CA generated under ``<system dir>/tls/``. Browsers reject
that chain until the CA is in the *device's* trust store, so every device that
connects needs a copy of it once. ``GET /ca.pem`` is how they get one —
including during after-setup's plain-HTTP wizard phase, where the CA already
exists and the Setup Wizard hands it over before the first HTTPS page is ever
loaded.

``/ca.pem`` must be unauthenticated. The moment a user meets the warning is
before they have logged in — often before the Setup Wizard has even run — and
the browser showing the warning is exactly the client that cannot present a
token. There is nothing to protect either way: a CA certificate is public
material, handed to every TLS client during the handshake. Trusting it grants
nothing except the ability to verify certificates signed by a private key that
never leaves the server.

The filename is hardcoded on purpose. ``ca.key``, ``cert.pem`` and ``key.pem``
sit in the same directory, so a parameterised path here would be a private-key
disclosure one traversal bug away.

``POST /api/tls/trust`` is the wizard's "Trust it on this device" button: on a
*native* install, the server process runs on the same machine and in the same
user session as the browser, so it can hand its own CA to the OS trust store
and spare the user the download-then-terminal dance. The layered guards below
(native-only, loopback-only, fingerprint echo) exist because "this device" is
a claim the server must verify, not assume — see ``post_trust``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.config.tls_auto import ca_fingerprint_sha256, tls_dir
from app.config.tls_trust import (
    already_trusted,
    render_command,
    run_trust_plan,
    server_trust_plan,
)


QUIESCE_ENROLLMENT_SECONDS = 0.75


async def get_ca_pem(_request: Request) -> FileResponse | JSONResponse:
    """Serve ``<system dir>/tls/ca.pem`` as a download.

    Gated on the file existing rather than on ``SSL_MODE``: the precedence
    between an explicit certificate pair, ``auto``, and the modes that disable
    TLS lives in ``server._resolve_tls``, and duplicating it here would only
    drift. Serving a CA while TLS happens to be off is harmless — it is public
    material either way — and it keeps the download working for a device that
    is being set up before the server is restarted with TLS on.
    """
    from app.config.tls_mode import _public_port, edge_tls_termination
    if edge_tls_termination() or _public_port() == 0 or (BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE):
        return JSONResponse({"error": "This server uses a supplied certificate. Obtain its CA from the certificate issuer."}, status_code=404)
    # Read the system dir per request: the Setup Wizard can relocate it, which
    # rebinds this attribute at runtime.
    ca_path = os.path.join(tls_dir(BaseConfig.CREMIND_SYSTEM_DIR), "ca.pem")
    if not os.path.isfile(ca_path):
        return JSONResponse(
            {
                "error": "No local CA on this server. One is generated at boot "
                         "when CREMIND_SSL is set to auto or after-setup."
            },
            status_code=404,
        )
    return FileResponse(
        ca_path,
        media_type="application/x-pem-file",
        # An explicit download name, rather than letting the browser render the
        # PEM inline: it is a file the user has to hand to a trust-store tool.
        # The name matches the anchor filename the Debian instructions use.
        filename="cremind-local-ca.pem",
        # The CA is regenerated if it expires or is deleted; revalidating each
        # time keeps a cached copy from masking that (FileResponse sends an
        # ETag and Last-Modified, so revalidation stays cheap).
        headers={"Cache-Control": "no-cache"},
    )


# ── server-side trust ─────────────────────────────────────────────────────

def _ca_path() -> str:
    return os.path.join(tls_dir(BaseConfig.CREMIND_SYSTEM_DIR), "ca.pem")


def _client_is_loopback(request: Request) -> bool:
    """Whether the TCP peer of this request is this machine itself.

    The public bind has no proxy in front of it on a native install, so the
    peer address is the browser's real address: loopback means the browser
    runs on the server's machine — the one topology where trusting the CA
    server-side lands on the right device. A LAN client of the same native
    install correctly fails this and is shown the manual commands instead.
    """
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _trust_environment_error() -> str | None:
    """Why server-side trust can never help here, or None if it can.

    In a container the store this process can write is the *container's*,
    which the browser on the host never consults; on Kubernetes the visiting
    device is another machine entirely. A native Electron child runs on the
    device itself and uses the same local trust guards as a native server.
    """
    from app.config.install_catalog import get_active_install_mode
    from app.config.tls_mode import _public_port, edge_tls_termination

    if _public_port() == 0 or edge_tls_termination():
        return "The reverse proxy owns HTTPS and its certificate. Trust the certificate issuer on this device."

    if BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE:
        return "This server uses a supplied certificate. Trust its issuer instead of a previous Cremind CA."

    mode = (get_active_install_mode() or "").strip().lower()
    if mode in ("docker", "kubernetes"):
        return (
            "This server runs in a container, so it can only write the "
            "container's trust store — not the one your browser uses. "
            "Trust the CA on your own machine instead."
        )
    return None


def local_trust_capabilities(request: Request) -> dict:
    """The ``local_trust`` sub-block of the capabilities ``tls`` payload.

    Computed per request on purpose: ``supported`` includes the loopback
    check, so a browser on another machine is never offered a button that
    would trust the CA on the wrong device.
    """
    ca_path = _ca_path()
    env_error = _trust_environment_error()
    from app.config.tls_mode import boot_serving_https
    if getattr(getattr(request, "url", None), "scheme", "http") == "https" and not boot_serving_https():
        env_error = "The reverse proxy owns the HTTPS certificate; this server's local CA is not used."
    plan = server_trust_plan(ca_path)
    supported = (
        env_error is None
        and _client_is_loopback(request)
        and plan.supported
        and os.path.isfile(ca_path)
    )
    return {
        "supported": supported,
        "store": plan.store if supported else None,
        "os_prompt": plan.os_prompt if supported else None,
        # Definitive only on Windows; None means "unknown", not "no".
        "already_trusted": already_trusted(ca_path) if supported else None,
        "reason": env_error or plan.reason,
    }


async def post_trust(request: Request) -> JSONResponse:
    """Install this server's CA into the trust store of *this* machine.

    Guard stack, each with a distinct job:

    - post-setup it requires the admin token (pre-setup it is open, like the
      rest of the wizard's bootstrap window);
    - container/Kubernetes installs are refused — the store this process can
      reach is not the one the browser consults (``_trust_environment_error``);
    - the TCP peer must be loopback, i.e. the browser really is on this
      machine;
    - requests with an Origin must be same-origin and the body must be JSON,
      which blocks cross-site form submissions during the open setup window;
    - the body must echo the CA's SHA-256 fingerprint, pinning the request to
      *this* CA so a stale page cannot trust one that has since been replaced.

    On Windows the OS shows its own confirmation dialog in the user's session
    before the root lands; a cancel there comes back as a tool failure and is
    reported honestly, with the manual commands to fall back on.
    """
    from app.api._auth import require_admin
    from app.runtime import get_state

    state = get_state()
    if state.storage_ready and state.config_storage.is_setup_complete():
        denied = require_admin(request)
        if denied is not None:
            return denied

    supplied_origin = request.headers.get("origin")
    if supplied_origin:
        from app.config.tls_transition import origin
        try:
            same_origin = origin(supplied_origin) == origin(str(request.base_url))
        except ValueError:
            same_origin = False
        if not same_origin:
            return JSONResponse(
                {"trusted": False, "error": "Certificate trust must be requested from this Cremind origin."},
                status_code=403,
            )
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse(
            {"trusted": False, "error": "Body must use application/json with ca_sha256."},
            status_code=415,
        )

    env_error = _trust_environment_error()
    if env_error is not None:
        return JSONResponse({"trusted": False, "error": env_error}, status_code=409)

    if not _client_is_loopback(request):
        return JSONResponse(
            {
                "trusted": False,
                "error": "This request did not come from the server's own "
                         "machine, so trusting here would land on the wrong "
                         "device. Run the trust command on your machine "
                         "instead.",
            },
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error
        body = None
    echoed = (body or {}).get("ca_sha256") if isinstance(body, dict) else None
    if not echoed or not isinstance(echoed, str):
        return JSONResponse(
            {"trusted": False, "error": "Body must be JSON with ca_sha256."},
            status_code=400,
        )

    ca_path = _ca_path()
    actual = ca_fingerprint_sha256(BaseConfig.CREMIND_SYSTEM_DIR)
    if actual is None:
        return JSONResponse(
            {"trusted": False, "error": "No local CA on this server."},
            status_code=404,
        )
    if echoed.strip().upper() != actual:
        return JSONResponse(
            {
                "trusted": False,
                "error": "The fingerprint you sent does not match this "
                         "server's CA — reload the page and try again.",
            },
            status_code=409,
        )

    plan = server_trust_plan(ca_path)
    if not plan.supported:
        return JSONResponse(
            {
                "trusted": False,
                "error": plan.reason,
                "manual_commands": [render_command(c) for c in plan.commands],
            },
            status_code=409,
        )

    # A re-run must not pop the OS dialog again for a CA that already landed.
    if already_trusted(ca_path):
        return JSONResponse(
            {"trusted": True, "already_trusted": True, "store": plan.store}
        )

    ok, error = run_trust_plan(plan)
    if not ok:
        return JSONResponse(
            {
                "trusted": False,
                "error": error,
                "manual_commands": [render_command(c) for c in plan.commands],
            },
            status_code=502,
        )
    return JSONResponse(
        {"trusted": True, "already_trusted": False, "store": plan.store}
    )


def get_tls_routes() -> list[Route]:
    return [
        Route("/ca.pem", get_ca_pem, methods=["GET"]),
        Route("/api/tls/trust", post_trust, methods=["POST"]),
        Route("/api/tls/status", get_tls_status, methods=["GET"]),
        Route("/api/tls/prepare", post_tls_prepare, methods=["POST"]),
        Route("/api/tls/activate", post_tls_activate, methods=["POST"]),
        Route("/api/tls/cancel", post_tls_cancel, methods=["POST"]),
        Route("/api/tls/client", post_tls_client, methods=["POST", "DELETE"]),
        Route("/api/tls/ready", post_tls_ready, methods=["POST"]),
        Route("/api/tls/handoff", post_tls_handoff, methods=["POST"]),
        Route("/api/tls/handoff/redeem", post_tls_redeem, methods=["POST"]),
    ]


def _request_origin(request: Request) -> str:
    from app.config.tls_transition import origin
    return origin(str(request.base_url))


def _source_origin(request: Request, supplied: str | None = None) -> str:
    from app.config.tls_transition import http_source
    # Local CLI talks to the dedicated HTTP port, which is never migrated.
    if not supplied and request.url.port == BaseConfig.PORT:
        return http_source(BaseConfig.APP_URL)
    result = http_source(supplied or _request_origin(request))
    if supplied and urlsplit(result).hostname != request.url.hostname and request.url.port != BaseConfig.PORT:
        raise ValueError("Use the hostname through which this device reaches Cremind.")
    return result


def _prepare(source: str, *, external: bool = False) -> dict:
    from app.config.tls_auto import ensure_local_tls
    from app.config.tls_transition import (
        certificate_info, https_target, instance_id, load_transition, management, port_facts, register_source, save_transition, validate_custom_certificate,
    )
    current = load_transition()
    if current and current["phase"] in ("prepared", "quiescing", "activating", "active"):
        return register_source(current, source)
    from app.config.tls_mode import _public_port, edge_tls_termination
    external = external or _public_port() == 0 or edge_tls_termination()
    if external:
        pass  # the proxy owns its certificate; never generate a misleading CA
    elif not (BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE):
        ensure_local_tls(BaseConfig.CREMIND_SYSTEM_DIR,
                         list(BaseConfig.SSL_AUTO_HOSTS) + [urlsplit(source).hostname or ""])
    else:
        validate_custom_certificate(urlsplit(source).hostname or "")
    return save_transition({
        "version": 1, "id": secrets.token_urlsafe(24), "phase": "prepared",
        "instance_id": instance_id(), "source_origin": source,
        "source_origins": [source], "target_origin": https_target(source),
        **certificate_info(external=external),
        **port_facts(external=external),
        "created_at": time.time(), "expires_at": None, "management": management(),
    })


def tls_status_payload(request: Request) -> dict:
    from app.config.tls_mode import current_tls_facts, edge_tls_termination
    from app.config.tls_transition import (
        certificate_info, https_target, instance_id, load_transition, management, mark_active, port_facts, public_transition, validate_custom_certificate,
    )
    facts = current_tls_facts()
    # ASGI scheme has already passed the server's trusted-proxy handling.
    # Never read X-Forwarded-Proto directly: an untrusted peer may forge it.
    edge_https = request.url.scheme == "https" and not facts.serving_https
    serving_https = facts.serving_https or edge_https
    certificate_error = None
    if not edge_https and not edge_tls_termination():
        try:
            validate_custom_certificate(urlsplit(_source_origin(request)).hostname or "")
        except ValueError as error:
            certificate_error = str(error)
    transition = load_transition()
    # Older after-setup installations still need a ticket BEFORE their first
    # restart. Public status creates metadata only; it never enables TLS.
    if not transition and facts.pending_https:
        transition = _prepare(_source_origin(request))
    if serving_https:
        mark_active(source=_source_origin(request), external=edge_https)
        transition = load_transition()
    mode = (os.environ.get("INSTALL_MODE") or "native").lower()
    manager = "external" if edge_https else management()
    instructions: list[str] = []
    if manager == "external":
        if edge_tls_termination():
            instructions = [
                "Configure the Ingress or reverse proxy's TLS certificate and secret for the public hostname; keep Cremind's in-pod SSL disabled.",
                "Update ingress.tls in your existing Helm values, including the HTTPS host and certificate secret.",
                "Allow the public HTTPS `/api/oauth/callback` URI in the Atlassian developer console before linking Jira or Confluence; set cremind.atlassianRedirectUri when it differs from the chart-derived URL.",
                "helm upgrade <release> <chart> --namespace <namespace> --reuse-values -f <your-values.yaml>",
                "kubectl rollout status deployment/<deployment> --namespace <namespace> --timeout=5m",
                "kubectl port-forward --namespace <namespace> svc/<service> 1515:80",
                "Set the HTTPS public APP_URL and preserve the public Host header. Forward the scheme only from explicitly trusted proxy addresses (FORWARDED_ALLOW_IPS); never trust arbitrary forwarded headers.",
                "Keep automatic HTTP redirects disabled so old HTTP document requests reach Cremind's session-recovery page. The recovery endpoint refuses plaintext API requests after activation.",
                "Apply the updated proxy, service and probe configuration together. Use the certificate issuer's trust instructions on every device; no Cremind CA is needed.",
            ]
        elif mode == "docker":
            instructions = [
                "In the host's Docker Compose .env, set CREMIND_SSL=true and change APP_URL to the HTTPS origin.",
                "For explicit CORS_ALLOWED_ORIGINS, retain the old HTTP origin and add the new HTTPS origin.",
                "Set CREMIND_ATLASSIAN_REDIRECT_URI to the HTTPS `/api/oauth/callback` URL and allow that exact URI in the Atlassian developer console before linking Jira or Confluence.",
                "Keep the system-directory volume mounted so the CA and transition survive container replacement.",
                "Run docker compose up -d --force-recreate cremind from the Compose project directory.",
            ]
        elif mode == "kubernetes":
            instructions = [
                "Set cremind.ssl=auto in your existing Helm values and use the HTTPS public origin for APP_URL.",
                "If cremind.atlassianRedirectUri is customized, change it to the HTTPS `/api/oauth/callback` URL and allow that exact URI in the Atlassian developer console.",
                "Run helm upgrade <release> <chart> --namespace <namespace> --reuse-values --set cremind.ssl=auto.",
                "kubectl rollout status deployment/<deployment> --namespace <namespace> --timeout=5m",
                "kubectl port-forward --namespace <namespace> svc/<service> 1515:80",
                "Use the updated chart so the application, relay, service and probes change together; retain your existing deployment values.",
                "Retain the system-directory PVC and keep the relay enabled for old HTTP URL recovery.",
                "Re-run the port-forward command if the tunnel closes during rollout.",
            ]
        else:
            instructions = [
                "Configure the reverse proxy's HTTPS certificate and HTTPS public APP_URL.",
                "Change CREMIND_ATLASSIAN_REDIRECT_URI to the public HTTPS `/api/oauth/callback` URL and allow that exact URI in the Atlassian developer console before linking Jira or Confluence.",
                "Keep the proxy's old HTTP endpoint forwarding recovery document requests to Cremind; refuse plaintext API writes.",
                "Reload the proxy after trusting its certificate on each client device.",
            ]
    elif not facts.restart_supported and manager != "electron":
        instructions = ["After activation, stop the current server process, then run `cremind serve` from the same installation to read the saved HTTPS settings."]
    migrated_atlassian = transition.get("atlassian_redirect_uri_migrated") if transition else None
    if isinstance(migrated_atlassian, str) and migrated_atlassian:
        instructions.append(
            "If Jira or Confluence account linking is configured, add the new "
            f"callback `{migrated_atlassian}` to the allowed redirect URI in "
            "the Atlassian developer console before linking an account."
        )
    expected = transition.get("quiesce_expected", {}) if transition else {}
    raw_acknowledged = transition.get("quiesce_acked", []) if transition else []
    acknowledged = (
        {item for item in raw_acknowledged if isinstance(item, str)}
        if isinstance(raw_acknowledged, list) else set()
    )
    quiesce_pending = (
        sum(tab_id not in acknowledged for tab_id in expected)
        if isinstance(expected, dict) else 0
    )
    return {
        "instance_id": instance_id(), "serving_https": serving_https,
        "ready": serving_https and certificate_error is None,
        "certificate_error": certificate_error,
        "mode": "custom" if BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE else facts.mode,
        "install_mode": mode,
        "management": manager, "electron_parent": os.environ.get("CREMIND_ELECTRON_PARENT") is not None,
        "restart_supported": facts.restart_supported or manager == "electron",
        "transition": public_transition(transition),
        "quiesce_pending": quiesce_pending,
        **certificate_info(external=edge_https),
        **port_facts(external=edge_https),
        "https_url": https_target(_source_origin(request)),
        "instructions": instructions, "local_trust": local_trust_capabilities(request),
    }


async def get_tls_status(request: Request) -> JSONResponse:
    return JSONResponse(tls_status_payload(request), headers={"Cache-Control": "no-store"})


async def _body(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 150000:
            raise ValueError("Request is too large.")
        raw.extend(chunk)
    value = json.loads(raw or b"{}")
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required.")
    return value


def _request_profile(request: Request) -> str:
    return str(getattr(request.user, "username", "") or "")


async def post_tls_client(request: Request) -> JSONResponse:
    """Register a live renderer that can preserve its own HTTPS session.

    The registry is process-local and contains opaque tab ids plus profile
    names only.  A quiescing transition copies its membership into the private,
    durable transition file; those ids are never included in public status or
    transport announcements.
    """
    from app.api._auth import require_auth
    from app.config import tls_clients

    denied = require_auth(request)
    if denied is not None:
        return denied
    try:
        data = await _body(request)
        profile = _request_profile(request)
        tab_id = tls_clients.validate_tab_id(data.get("tab_id"))
        if request.method == "DELETE":
            tls_clients.unregister(tab_id, profile)
        else:
            # Tabs opening during the short enrollment interval join the same
            # round.  Closing enrollment and adding members are serialized by
            # update_transition, so activation cannot miss a concurrent join.
            from app.config.tls_transition import load_transition, register_source, update_transition
            current = load_transition()
            if current and current.get("phase") in ("prepared", "quiescing"):
                # Discover the exact hostname on authenticated heartbeats,
                # before an admin captures the leaf fingerprint for activation.
                # No source is accepted from JSON; this comes from the actual
                # request origin (or canonical APP_URL on the private CLI port).
                current = register_source(current, _source_origin(request))
            # Register only after source preparation succeeds. Otherwise a
            # rejected alias could leave an un-migratable tab in the quiesce
            # snapshot and permanently block this activation attempt.
            tls_clients.register(tab_id, profile)
            if current and current.get("phase") == "quiescing":
                def enroll(value: dict) -> dict:
                    if (value.get("phase") == "quiescing"
                            and not value.get("quiesce_closed")
                            and time.time() <= float(value.get("quiesce_enrollment_until", 0))):
                        expected = value.setdefault("quiesce_expected", {})
                        if isinstance(expected, dict):
                            expected[tab_id] = profile
                    return value
                update_transition(enroll, announce=False)
        return JSONResponse(tls_status_payload(request), headers={"Cache-Control": "no-store"})
    except (ValueError, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_ready(request: Request) -> JSONResponse:
    """A renderer confirms that uploads settled and its handoff is private."""
    from app.api._auth import require_auth
    from app.config import tls_clients
    from app.config.tls_transition import update_transition

    denied = require_auth(request)
    if denied is not None:
        return denied
    try:
        data = await _body(request)
        profile = _request_profile(request)
        tab_id = tls_clients.validate_tab_id(data.get("tab_id"))
        transition_id = data.get("transition_id")

        def acknowledge(value: dict) -> dict:
            if value.get("id") != transition_id or value.get("phase") != "quiescing":
                raise ValueError("This HTTPS preparation round is no longer accepting readiness.")
            expected = value.get("quiesce_expected", {})
            if not isinstance(expected, dict) or expected.get(tab_id) != profile:
                raise PermissionError("This tab is not enrolled for the authenticated profile.")
            acknowledged = value.setdefault("quiesce_acked", [])
            if (not isinstance(acknowledged, list)
                    or any(not isinstance(item, str) for item in acknowledged)):
                raise ValueError("HTTPS readiness metadata is invalid. Cancel and prepare again.")
            if tab_id not in acknowledged:
                acknowledged.append(tab_id)
            return value

        update_transition(acknowledge, announce=False)
        return JSONResponse(tls_status_payload(request), headers={"Cache-Control": "no-store"})
    except PermissionError as error:
        return JSONResponse({"error": str(error)}, status_code=403)
    except (ValueError, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_prepare(request: Request) -> JSONResponse:
    from app.api._auth import require_admin
    denied = require_admin(request)
    if denied is not None:
        return denied
    try:
        data = await _body(request)
        from app.config.tls_mode import boot_serving_https
        _prepare(_source_origin(request, data.get("source_origin")),
                 external=request.url.scheme == "https" and not boot_serving_https())
        return JSONResponse(tls_status_payload(request), headers={"Cache-Control": "no-store"})
    except (ValueError, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_activate(request: Request) -> JSONResponse:
    from app.api._auth import require_admin
    from app.auth.tokens import current_transport_epoch, reissue_token_files_for_epoch
    from app.config.tls_transition import (
        UPLOAD_RECOVERY_TTL,
        certificate_info,
        load_transition,
        management,
        persist_native,
        save_transition,
        update_transition,
        validate_custom_certificate,
    )
    denied = require_admin(request)
    if denied is not None:
        return denied
    try:
        data = await _body(request)
        value = load_transition()
        if not value or value["id"] != data.get("transition_id") or value["phase"] == "cancelled":
            raise ValueError("Prepare HTTPS before activating it.")
        info = certificate_info(external=value.get("certificate_kind") == "external")
        if value.get("certificate_kind") != "external":
            for source in value.get("source_origins", [value["source_origin"]]):
                validate_custom_certificate(urlsplit(source).hostname or "")
        fingerprint = value.get("certificate_sha256", value.get("ca_sha256"))
        if fingerprint != info["certificate_sha256"]:
            raise ValueError("The certificate changed. Cancel and prepare HTTPS again before trusting it.")
        echoed = data.get("certificate_sha256") or data.get("ca_sha256")
        if fingerprint is not None and echoed != fingerprint:
            raise ValueError("Confirm the current certificate fingerprint before activating HTTPS.")
        if value["phase"] in ("activating", "active"):
            return JSONResponse({**tls_status_payload(request), "restart_required": value["phase"] != "active"})
        if value["phase"] not in ("prepared", "quiescing"):
            raise ValueError("Prepare HTTPS before activating it.")

        # Keep the old token epoch and full HTTP application alive while every
        # registered renderer finishes its uploads and creates a private
        # handoff.  This also covers activation initiated by the CLI, where no
        # in-page BroadcastChannel barrier can run before the request.
        started_quiesce = value["phase"] == "prepared"
        if started_quiesce:
            from app.config.tls_clients import snapshot
            value["phase"] = "quiescing"
            value["quiesce_expected"] = snapshot()
            value["quiesce_acked"] = []
            value["quiesce_closed"] = False
            value["quiesce_enrollment_until"] = time.time() + QUIESCE_ENROLLMENT_SECONDS
            save_transition(value)
            # Take a second snapshot after publishing quiescing. A client that
            # registered between the first snapshot and the durable write saw
            # the previous phase, so its endpoint could not enroll itself.
            registered = snapshot()

            def enroll_snapshot(current: dict) -> dict:
                if current.get("id") != data.get("transition_id") or current.get("phase") != "quiescing":
                    raise ValueError("The HTTPS transition changed while tabs were enrolling.")
                expected = current.setdefault("quiesce_expected", {})
                if not isinstance(expected, dict):
                    raise ValueError("HTTPS readiness metadata is invalid. Cancel and prepare again.")
                expected.update(registered)
                return current

            value = update_transition(enroll_snapshot, announce=False)
            # Existing tabs need the response/SSE frame before they can ACK.
            # With none registered, briefly admit a concurrently mounting tab,
            # then finish in this same request so unattended installs remain
            # a single API operation.
            if value["quiesce_expected"]:
                return JSONResponse(
                    {**tls_status_payload(request), "restart_required": False,
                     "restart_scheduled": False},
                    status_code=202,
                    headers={"Cache-Control": "no-store"},
                )
            await asyncio.sleep(max(0.0, QUIESCE_ENROLLMENT_SECONDS))

        value = load_transition()
        if not value or value.get("id") != data.get("transition_id"):
            raise ValueError("The HTTPS transition changed while tabs were preparing.")
        if value.get("phase") != "quiescing":
            if value.get("phase") in ("activating", "active"):
                return JSONResponse({**tls_status_payload(request),
                                     "restart_required": value["phase"] != "active"})
            raise ValueError("The HTTPS transition is no longer being prepared.")
        if time.time() < float(value.get("quiesce_enrollment_until", 0)):
            return JSONResponse(
                {**tls_status_payload(request), "restart_required": False,
                 "restart_scheduled": False},
                status_code=202,
                headers={"Cache-Control": "no-store"},
            )

        def close_quiesce(current: dict) -> dict:
            if current.get("id") != data.get("transition_id") or current.get("phase") != "quiescing":
                raise ValueError("The HTTPS transition changed while tabs were preparing.")
            current["quiesce_closed"] = True
            return current

        value = update_transition(close_quiesce, announce=False)
        expected = value.get("quiesce_expected", {})
        acknowledged = value.get("quiesce_acked", [])
        if (not isinstance(expected, dict) or not isinstance(acknowledged, list)
                or any(not isinstance(item, str) for item in acknowledged)):
            raise ValueError("HTTPS readiness metadata is invalid. Cancel and prepare again.")
        pending = set(expected).difference(acknowledged)
        if pending:
            return JSONResponse(
                {**tls_status_payload(request), "restart_required": False,
                 "restart_scheduled": False},
                status_code=202,
                headers={"Cache-Control": "no-store"},
            )

        def restore_prepared() -> None:
            value["phase"] = "prepared"
            # Mark this as a new preparation attempt even though the stable
            # transition id is retained for the admin's retry request.  Tabs
            # use the newer timestamp to distinguish this intentional rollback
            # from a delayed pre-quiesce SSE frame and release their old gates.
            previous_created_at = value.get("created_at", 0)
            if not isinstance(previous_created_at, (int, float)):
                previous_created_at = 0
            value["created_at"] = max(time.time(), previous_created_at + 0.000001)
            for key in ("quiesce_expected", "quiesce_acked", "quiesce_closed",
                        "quiesce_enrollment_until", "transport_epoch",
                        "upload_recovery_until", "atlassian_redirect_uri_migrated"):
                value.pop(key, None)
            try:
                save_transition(value)
            except Exception:
                # Preserve the original persistence error. The previous
                # transition file remains complete and restart-safe.
                pass

        manager = management()
        try:
            rollback = persist_native(value) if manager != "external" else None
        except Exception:
            restore_prepared()
            raise
        current_epoch = current_transport_epoch()
        if current_epoch is None:
            if rollback:
                rollback()
            restore_prepared()
            raise OSError("TLS transition metadata is unreadable; HTTPS activation was not started.")
        # Reissue the on-host recovery/CLI credentials before publishing the
        # new epoch. Browser sessions already hold private handoff tickets from
        # the preparation barrier and are re-signed only when redeemed on HTTPS.
        token_rollback = None
        try:
            token_rollback = reissue_token_files_for_epoch(current_epoch + 1)
        except Exception:
            if rollback:
                rollback()
            restore_prepared()
            raise
        for key in ("quiesce_expected", "quiesce_acked", "quiesce_closed",
                    "quiesce_enrollment_until"):
            value.pop(key, None)
        value["phase"] = "activating"
        value["transport_epoch"] = current_epoch + 1
        value["upload_recovery_until"] = time.time() + UPLOAD_RECOVERY_TTL
        try:
            save_transition(value)  # durable + published BEFORE shutdown
        except Exception:
            if token_rollback:
                token_rollback()
            if rollback:
                rollback()
            restore_prepared()
            raise
        from app.config.tls_clients import clear as clear_tls_clients
        clear_tls_clients()
        restart_scheduled = False
        restart_error = None
        if data.get("restart", True) is not False:
            from app.config.tls_mode import current_tls_facts
            restart_facts = current_tls_facts()
            # Native services may restart any upgrade after their canonical
            # env is persisted. Docker/Kubernetes may self-restart only during
            # the install-time after-setup flow, where their deployment was
            # already rendered for TLS. A later external upgrade must recreate
            # the container or Helm release so proxy/Service/probes move too.
            # Electron always owns its child process in main, including the
            # install-time after-setup switch.
            can_schedule = restart_facts.restart_supported and (
                manager == "native"
                or (manager == "external" and restart_facts.pending_https)
            )
            if can_schedule:
                try:
                    from app.api.system import schedule_system_restart
                    schedule_system_restart()
                    restart_scheduled = True
                except OSError as error:
                    restart_error = (
                        "HTTPS was saved, but the supervised restart could not "
                        f"be scheduled: {error}. Run `cremind server restart --yes` "
                        "from this installation."
                    )
        status = {
            **tls_status_payload(request),
            "restart_required": not restart_scheduled,
            "restart_scheduled": restart_scheduled,
            "restart_error": restart_error,
        }
        if restart_error:
            status["instructions"] = [*status.get("instructions", []), restart_error]
        # Electron owns process lifetime through the main-process coordinator;
        # containers and Kubernetes use the deployment commands in this status.
        return JSONResponse(status, status_code=202, headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_cancel(request: Request) -> JSONResponse:
    from app.api._auth import require_admin
    from app.config.tls_transition import load_transition, save_transition
    denied = require_admin(request)
    if denied is not None:
        return denied
    try:
        data = await _body(request)
        value = load_transition()
        if not value or value["id"] != data.get("transition_id"):
            raise ValueError("The HTTPS transition was not found.")
        if value["phase"] not in ("prepared", "quiescing", "cancelled"):
            return JSONResponse({"error": "HTTPS activation has started. Use the recovery instructions to finish it."}, status_code=409)
        expected = value.get("quiesce_expected", {})
        raw_acknowledged = value.get("quiesce_acked", [])
        acknowledged = (
            {item for item in raw_acknowledged if isinstance(item, str)}
            if isinstance(raw_acknowledged, list) else set()
        )
        unresponsive = set(expected).difference(acknowledged) if isinstance(expected, dict) else set()
        value["phase"] = "cancelled"
        for key in ("quiesce_expected", "quiesce_acked", "quiesce_closed",
                    "quiesce_enrollment_until"):
            value.pop(key, None)
        save_transition(value)
        from app.config.tls_clients import remove as remove_tls_clients
        remove_tls_clients(unresponsive)
        return JSONResponse(tls_status_payload(request))
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_handoff(request: Request) -> JSONResponse:
    from app.auth.tokens import verify_token
    from app.config.tls_transition import https_target, load_transition, mint_ticket, origin, register_source
    try:
        data = await _body(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not verify_token(token):
            raise PermissionError("A valid profile session is required.")
        source = origin(data.get("source_origin", ""))
        # A restored tab may use another certificate-valid alias of the same
        # server. Register only this request's exact HTTP counterpart after
        # authenticating, never an arbitrary source supplied in JSON.
        request_source = origin(_request_origin(request), scheme="http")
        transition = load_transition()
        if transition and (source == request_source or https_target(source) == _request_origin(request)):
            register_source(transition, source)
        return JSONResponse(mint_ticket(token, data), headers={"Cache-Control": "no-store"})
    except PermissionError as error:
        return JSONResponse({"error": str(error)}, status_code=401)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_redeem(request: Request) -> JSONResponse:
    from app.config.tls_transition import HandoffSessionExpired, redeem_ticket
    if request.url.scheme != "https":
        return JSONResponse({"error": "Session handoffs can only be received over HTTPS."}, status_code=403)
    try:
        data = await _body(request)
        return JSONResponse(redeem_ticket(data.get("ticket"), _request_origin(request)),
                            headers={"Cache-Control": "no-store"})
    except HandoffSessionExpired as error:
        return JSONResponse({"error": str(error), "profile": error.profile, "route": error.route},
                            status_code=401, headers={"Cache-Control": "no-store"})
    except PermissionError as error:
        return JSONResponse({"error": str(error)}, status_code=401)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
