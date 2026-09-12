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
    # Local CLI talks to the dedicated HTTP port, which is never migrated, so
    # the public address has to come from the configuration instead of the
    # request — repaired, because an APP_URL naming that same internal port
    # would otherwise make the CLI prepare a switch to an origin no browser
    # can open (see ``public_app_url``).
    if not supplied and request.url.port == BaseConfig.PORT:
        from app.config.tls_mode import public_app_url
        return http_source(public_app_url())
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


def _switch_blocker() -> str | None:
    """A reason this switch cannot work, checked while nothing has changed yet.

    Activation already proves the certificate end to end — it re-reads the
    fingerprint, refuses a certificate that moved since preparation, and
    validates a supplied pair against every origin that joined the switch. What
    it never checked is the *address*, and that is the one input the switch
    derives rather than verifies: ``persist_native`` turns ``APP_URL`` into the
    HTTPS origin it writes into the agent card, the OAuth redirects and the
    Atlassian callback. An unreachable value there survives the switch and
    breaks account linking afterwards, long after the cause is obvious.

    Wherever Cremind persists that setting itself — a native or Electron
    install, and a Compose install whose settings live in the system-directory
    volume — it repairs the value instead of refusing (``public_app_url``):
    refusing there would tell an administrator to go and edit a file by hand,
    which is the very thing those deployments exist to avoid. An ``external``
    deployment keeps its own environment, so Cremind writes nothing, has nothing
    to repair, and has to say so.

    Returns the message to refuse with, or ``None`` to proceed.
    """
    from app.config.tls_mode import _public_port, app_url_names_internal_bind
    from app.config.tls_transition import management

    if management() == "external" and app_url_names_internal_bind(BaseConfig.PORT):
        return (
            f"APP_URL is {BaseConfig.APP_URL!r}, but port {BaseConfig.PORT} is the "
            "internal API bind — it listens on 127.0.0.1 only and is never published, "
            "so no browser can open it. Switching to HTTPS would derive the new public "
            "origin, the Google and Atlassian callbacks and the agent card from that "
            "address. Cremind does not own this deployment's environment, so set "
            f"APP_URL to the address browsers actually use (port {_public_port()}) "
            "where the deployment defines it — Kubernetes: `--set cremind.appUrl=…`; "
            "reverse proxy: APP_URL in Cremind's .env — and restart before activating "
            "HTTPS."
        )
    return None


def _cancellable(request: Request, transition: dict | None) -> bool:
    """Whether this switch can still be called off through this request.

    Cancel is the promise that nothing has been invalidated yet, so it stops
    being offered the moment HTTPS genuinely serves. It is also withheld while a
    restart is already armed: the native supervisor and Electron's coordinator
    stop consulting the transition file once they have seen ``activating``, so a
    cancel accepted then would be reverted underneath them.
    """
    from app.config.tls_mode import _public_port, boot_serving_https, edge_tls_termination
    from app.config.tls_transition import awaiting_operator, management, requires_confirmation
    if not transition:
        return False
    if transition["phase"] in ("prepared", "quiescing"):
        return True
    if not awaiting_operator(transition):
        return False
    # Binding TLS is not the same as completing the switch. While a self-applied
    # switch is still waiting to be confirmed, the boundary has not moved and no
    # session has been invalidated, so the way back stays open even though this
    # process is serving HTTPS — that plaintext tab asking to cancel is exactly
    # the administrator who could not reach the new origin.
    if not requires_confirmation(transition):
        if request.url.scheme == "https" or boot_serving_https():
            return False
    # On a deployment-managed install the switch can land in front of this
    # process without it ever knowing, and cancelling then would leave HTTPS
    # serving on the old boundary forever. Both signals below mean exactly
    # that: an explicitly configured terminator, or a process with no public
    # bind of its own — a reverse proxy owns the origin, so this server cannot
    # observe the transport and ``APP_URL`` is the only evidence there is.
    #
    # ``APP_URL`` alone is NOT that evidence. A process holding its own public
    # bind knows what it is serving, and ``boot_serving_https()`` above has
    # already answered: still plaintext. Treating an https ``APP_URL`` as proof
    # there told a Docker operator whose CREMIND_SSL had not taken effect that
    # "HTTPS is already being served" — refusing the one action that would have
    # given them their install back, while HTTP was demonstrably serving them
    # the refusal.
    if management() == "external" and (
            edge_tls_termination()
            or (_public_port() == 0 and BaseConfig.APP_URL.startswith("https://"))):
        return False
    return True


def tls_status_payload(request: Request) -> dict:
    from app.api._auth import is_admin
    from app.config import runtime_env
    from app.config.tls_mode import current_tls_facts, edge_tls_termination
    from app.config.tls_steps import (
        atlassian_callback_step, certificate_repair_steps, deployment_steps, flatten,
        running_chart_version,
    )
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
        # An authenticated request carried over the new transport is what
        # confirms a self-applied switch. The recovery page polls this same
        # endpoint cross-origin with credentials omitted: that proves TLS
        # reachability but not that anyone can still *use* the install, so it
        # deliberately does not count. Requiring the admin session keeps the
        # proof and the person who would have to undo the switch the same one.
        mark_active(source=_source_origin(request), external=edge_https,
                    confirmed=request.url.scheme == "https" and is_admin(request))
        transition = load_transition()
    mode = (os.environ.get("INSTALL_MODE") or "native").lower()
    manager = "external" if edge_https else management()
    https_url = https_target(_source_origin(request))
    # What this pod is, when it is one: the runbook prints the real namespace,
    # release and Deployment instead of placeholders the operator has to look
    # up and substitute by hand. ``kubernetes_identity`` is deliberately
    # uncached, and every name it returns is already validated as a Kubernetes
    # object name - an ``extraEnv`` value carrying a newline would otherwise
    # make ``command()`` raise and turn this endpoint into a 500.
    #
    # Admin-only, and the gate is here rather than on the route: this endpoint
    # must keep answering without a token - the plaintext recovery page and the
    # pre-sign-in wizard both poll it - so it is the *payload* that has to know
    # who is asking. Cluster topology and ready-to-run kubectl/helm lines are
    # what ``/api/system/environment`` and ``/api/config/install-secrets`` keep
    # behind admin auth, so publishing them to anyone who can reach the port
    # would undo that. Everyone else gets exactly the runbook a chart too old
    # to state its own names produces: placeholders, plus ``helm list`` to find
    # what to substitute.
    identity = (
        runtime_env.kubernetes_identity(mode)
        if mode == "kubernetes" and is_admin(request)
        else None
    )
    # Steps are a typed runbook (note vs command) so that only real shell lines
    # get a copy button; ``instructions`` below stays as its flat rendering for
    # clients older than that split.  A server already on HTTPS has nothing to
    # enable, so it offers repair steps or nothing at all.
    if serving_https:
        steps = certificate_repair_steps(
            manager=manager, install_mode=mode,
            restart_supported=facts.restart_supported, kubernetes=identity,
        ) if certificate_error else []
    else:
        steps = deployment_steps(
            manager=manager, install_mode=mode, edge=edge_tls_termination(),
            restart_supported=facts.restart_supported,
            activating=bool(transition and transition.get("phase") == "activating"),
            https_url=https_url, chart_version=running_chart_version(),
            kubernetes=identity,
        )
    migrated_atlassian = transition.get("atlassian_redirect_uri_migrated") if transition else None
    if isinstance(migrated_atlassian, str) and migrated_atlassian:
        steps.append(atlassian_callback_step(migrated_atlassian))
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
        # ``None`` everywhere but an admin's view of a Kubernetes install (see
        # above). The steps already read it; it rides the payload too so a
        # client can label the runbook with the release it is about (and say
        # when the names were only inferred).
        "kubernetes": identity,
        "management": manager, "electron_parent": os.environ.get("CREMIND_ELECTRON_PARENT") is not None,
        "restart_supported": facts.restart_supported or manager == "electron",
        "transition": public_transition(transition),
        "quiesce_pending": quiesce_pending,
        "can_cancel": _cancellable(request, transition),
        "activation_error": transition.get("activation_error") if transition else None,
        **certificate_info(external=edge_https),
        **port_facts(external=edge_https),
        "https_url": https_url,
        "steps": steps, "instructions": flatten(steps),
        "local_trust": local_trust_capabilities(request),
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
            # A tab that says goodbye cannot acknowledge a readiness round any
            # more, and an expectation nobody can meet holds the switch in
            # ``quiescing`` until someone cancels it. Closing the tab that is
            # blocking the switch is the obvious remedy, so make it work.
            #
            # Only when this tab really is holding one up: every tab close comes
            # through here, and neither a rewrite of the durable transition nor
            # (with no transition at all) update_transition's refusal belongs on
            # that path.
            from app.config.tls_transition import load_transition, update_transition

            current = load_transition() or {}
            expected_now = current.get("quiesce_expected")
            if (current.get("phase") == "quiescing" and isinstance(expected_now, dict)
                    and expected_now.get(tab_id) == profile):
                def withdraw(value: dict) -> dict:
                    # Re-checked under the lock: the round may have closed while
                    # this request waited for it.
                    if value.get("phase") == "quiescing":
                        expected = value.get("quiesce_expected")
                        if isinstance(expected, dict) and expected.get(tab_id) == profile:
                            expected.pop(tab_id, None)
                    return value

                try:
                    update_transition(withdraw, announce=False)
                except ValueError:
                    pass  # the switch ended between the read and the lock
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
    from app.auth.tokens import current_transport_epoch, preflight_token_files
    from app.config.tls_transition import (
        ACTIVATION_KEYS,
        CONFIRMATION_DEADLINE_SECONDS,
        UPLOAD_RECOVERY_TTL,
        certificate_info,
        discard_native_rollback,
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
        blocker = _switch_blocker()
        if blocker:
            raise ValueError(blocker)

        # Keep the old token epoch and full HTTP application alive while every
        # registered renderer finishes its uploads and creates a private
        # handoff.  This also covers activation initiated by the CLI, where no
        # in-page BroadcastChannel barrier can run before the request.
        # Only clients still heartbeating are held to an acknowledgement: a tab
        # that went away without unregistering (a closed or crashed browser)
        # would otherwise keep every future activation in ``quiescing`` until
        # someone cancelled the switch. See app/config/tls_clients.py.
        started_quiesce = value["phase"] == "prepared"
        if started_quiesce:
            from app.config.tls_clients import LIVE_WITHIN_SECONDS, snapshot
            value["phase"] = "quiescing"
            value["quiesce_expected"] = snapshot(LIVE_WITHIN_SECONDS)
            value["quiesce_acked"] = []
            value["quiesce_closed"] = False
            value["quiesce_enrollment_until"] = time.time() + QUIESCE_ENROLLMENT_SECONDS
            save_transition(value)
            # Take a second snapshot after publishing quiescing. A client that
            # registered between the first snapshot and the durable write saw
            # the previous phase, so its endpoint could not enroll itself.
            registered = snapshot(LIVE_WITHIN_SECONDS)

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
            # ``transport_epoch`` is not in this list: only a completed advance
            # ever writes it, and it must never move backwards.
            for key in ("quiesce_expected", "quiesce_acked", "quiesce_closed",
                        "quiesce_enrollment_until", *ACTIVATION_KEYS):
                value.pop(key, None)
            try:
                save_transition(value)
            except Exception:
                # Preserve the original persistence error. The previous
                # transition file remains complete and restart-safe.
                pass
            discard_native_rollback()

        manager = management()
        current_epoch = current_transport_epoch()
        if current_epoch is None:
            restore_prepared()
            raise OSError("TLS transition metadata is unreadable; HTTPS activation was not started.")
        # The credential boundary moves when HTTPS first serves, not here (see
        # ``advance_transport_epoch``): until the deployment change lands, the
        # HTTP application has to keep working or its administrator is locked
        # away from their own data. Provoke now, while this request can still
        # abort with nothing changed, the failures that would otherwise strand
        # the on-host credentials at that later, unattended moment.
        try:
            preflight_token_files()
        except OSError:
            restore_prepared()
            raise
        try:
            rollback = persist_native(value) if manager != "external" else None
        except Exception:
            restore_prepared()
            raise
        # Native services may restart any upgrade after their canonical env is
        # persisted, and a managed Compose install now may too: its settings went
        # into the system-directory volume, which the next boot reads, so the
        # restart the container already performs for itself is enough and nothing
        # has to be recreated. A Kubernetes release still may not — enabling TLS
        # there moves the Service, the probes and the proxy sidecar together, so
        # only a Helm upgrade can do it, and it may self-restart solely during
        # the install-time after-setup flow where the chart already rendered for
        # TLS. Electron always owns its child process in main, including that
        # same install-time switch. Decided before the durable write so the first
        # announcement already tells tabs whether anyone is coming to finish this.
        restart_requested = data.get("restart", True) is not False
        from app.config.tls_mode import current_tls_facts
        restart_facts = current_tls_facts()
        can_schedule = restart_facts.restart_supported and (
            manager in ("native", "managed-docker")
            or (manager == "external" and restart_facts.pending_https)
        )
        for key in ("quiesce_expected", "quiesce_acked", "quiesce_closed",
                    "quiesce_enrollment_until"):
            value.pop(key, None)
        value["phase"] = "activating"
        value["pending_transport_epoch"] = current_epoch + 1
        value["restart_planned"] = (restart_requested and can_schedule) or manager == "electron"
        value["activated_at"] = time.time()
        value["upload_recovery_until"] = time.time() + UPLOAD_RECOVERY_TTL
        # Cremind persisted this change and is about to restart the process
        # itself, so it holds both halves of an undo nobody else can perform.
        # That earns the switch a stricter completion rule — the boundary waits
        # for a client that genuinely reached HTTPS — and, in exchange, a
        # deadline after which the installation puts itself back.
        #
        # Excluded, deliberately: an external manager (no rollback record was
        # written, so there is nothing to undo), ``--no-restart`` (the operator
        # took over the timing and must not have it reverted underneath them),
        # and Electron (its main process owns the child, so a revert-and-exit
        # here could leave the app with no backend at all).
        value["self_applied"] = bool(
            rollback and value["restart_planned"] and manager != "electron"
        )
        if value["self_applied"]:
            value["confirmation_deadline"] = time.time() + CONFIRMATION_DEADLINE_SECONDS
        try:
            save_transition(value)  # durable + published BEFORE shutdown
        except Exception:
            if rollback:
                rollback()
            restore_prepared()
            raise
        from app.config.tls_clients import clear as clear_tls_clients
        clear_tls_clients()
        restart_scheduled = False
        restart_error = None
        if restart_requested and can_schedule:
            try:
                from app.api.system import schedule_system_restart
                schedule_system_restart()
                restart_scheduled = True
            except OSError as error:
                restart_error = (
                    "HTTPS was saved, but the supervised restart could not "
                    f"be scheduled: {error}."
                )
                # Nothing is coming to finish this, so say so: the runbook and
                # the cancel button are gated on it. The self-applied promise
                # goes with it — no restart means no HTTPS to confirm, and a
                # deadline would revert a switch that never got to start.
                try:
                    update_transition(lambda current: {
                        **{key: item for key, item in current.items()
                           if key not in ("self_applied", "confirmation_deadline")},
                        "restart_planned": False,
                    })
                    value = load_transition() or value
                except (ValueError, OSError):
                    pass
        status = {
            **tls_status_payload(request),
            "restart_required": not restart_scheduled,
            "restart_scheduled": restart_scheduled,
            "restart_error": restart_error,
        }
        if restart_error:
            # The recovery command is its own step, so it stays copy-pasteable;
            # the flat list keeps both parts for clients that read only it.
            from app.config.tls_steps import RESTART_COMMAND, command
            status["steps"] = [*status.get("steps", []), command(RESTART_COMMAND)]
            status["instructions"] = [
                *status.get("instructions", []), restart_error, RESTART_COMMAND,
            ]
        # Electron owns process lifetime through the main-process coordinator;
        # containers and Kubernetes use the `steps` in this status.
        return JSONResponse(status, status_code=202, headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def post_tls_cancel(request: Request) -> JSONResponse:
    """Call off a switch that has not invalidated anything yet.

    Available through the whole preparation *and* while activation waits for a
    deployment change that has not landed: that window can last hours, and
    without a way out of it an operator who changed their mind — or whose Helm
    upgrade will never come — has no route back to their own data.
    """
    from app.api._auth import require_admin
    from app.config.tls_transition import (
        ACTIVATION_KEYS,
        discard_native_rollback,
        load_transition,
        management,
        revert_native,
        save_transition,
    )
    denied = require_admin(request)
    if denied is not None:
        return denied
    try:
        data = await _body(request)
        value = load_transition()
        if not value or value["id"] != data.get("transition_id"):
            raise ValueError("The HTTPS transition was not found.")
        if value["phase"] != "cancelled" and not _cancellable(request, value):
            from app.config.tls_transition import awaiting_operator
            message = (
                "A restart into HTTPS is already scheduled for this switch; wait "
                "for it to finish."
                if value["phase"] == "activating" and not awaiting_operator(value)
                else "HTTPS is already being served for this switch, so it can no "
                     "longer be cancelled. Open the HTTPS address to continue."
            )
            return JSONResponse({"error": message}, status_code=409)
        expected = value.get("quiesce_expected", {})
        raw_acknowledged = value.get("quiesce_acked", [])
        acknowledged = (
            {item for item in raw_acknowledged if isinstance(item, str)}
            if isinstance(raw_acknowledged, list) else set()
        )
        unresponsive = set(expected).difference(acknowledged) if isinstance(expected, dict) else set()
        activating = value["phase"] == "activating"
        # Cancel is now reachable on a process that has ALREADY bound TLS:
        # ``_cancellable`` allows it while a self-applied switch is unconfirmed,
        # which is exactly the administrator who could not reach the new origin.
        # That process cannot unbind TLS, and the moment the phase stops being
        # ``activating`` the relay takes plaintext back to the recovery page —
        # so without a restart the cancel would revert the configuration, leave
        # HTTPS serving the old boundary anyway, and close the very surface the
        # request arrived on. Read before ACTIVATION_KEYS destroys the evidence.
        from app.config.tls_mode import boot_serving_https
        from app.config.tls_transition import requires_confirmation
        restart_onto_http = boot_serving_https() and requires_confirmation(value)
        value["phase"] = "cancelled"
        for key in ("quiesce_expected", "quiesce_acked", "quiesce_closed",
                    "quiesce_enrollment_until", *ACTIVATION_KEYS):
            value.pop(key, None)
        save_transition(value)
        from app.config.tls_clients import remove as remove_tls_clients
        remove_tls_clients(unresponsive)
        # After the durable phase change: a failed restore is worth reporting,
        # but it must not leave the switch half-cancelled.
        revert_error = None
        if activating and management() != "external":
            try:
                revert_native(value["id"])
            except OSError as error:
                revert_error = (
                    f"The switch was cancelled, but the previous settings could not "
                    f"be restored: {error} Check CREMIND_SSL and APP_URL in the "
                    f"system directory's .env before restarting."
                )
        else:
            discard_native_rollback()
        restart_scheduled = False
        if restart_onto_http and revert_error is None:
            try:
                from app.api.system import schedule_system_restart

                schedule_system_restart()
                restart_scheduled = True
            except OSError as error:
                revert_error = (
                    "The switch was cancelled and the previous settings restored, but "
                    f"the restart back onto HTTP could not be scheduled: {error} This "
                    "server is still serving HTTPS until it is restarted by hand."
                )
        return JSONResponse({**tls_status_payload(request), "revert_error": revert_error,
                             "restart_scheduled": restart_scheduled})
    except (ValueError, OSError) as error:
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
    from app.config.tls_mode import boot_serving_https
    from app.config.tls_transition import (
        HandoffSessionExpired, mark_active, redeem_ticket, ticket_exists,
    )
    if request.url.scheme != "https":
        return JSONResponse({"error": "Session handoffs can only be received over HTTPS."}, status_code=403)
    try:
        data = await _body(request)
        # HTTPS is demonstrably serving, so move the boundary before re-signing
        # anything: a status call normally does it first, but redemption must
        # not depend on that ordering or it would hand back a token for an
        # epoch that is about to be retired.
        #
        # A redemption also confirms a self-applied switch — but the *ticket* is
        # the proof, not the scheme. This route is unauthenticated, the https
        # check above is satisfied by any client that skips certificate
        # validation, and on the loopback bind it is satisfied by a forged
        # X-Forwarded-Proto. An empty body would otherwise advance the boundary
        # and delete the rollback record, destroying every way back from a
        # switch nobody can reach. Minting a ticket needs a valid session, so
        # holding one is the evidence; an expired one still proves the origin
        # works, which is why the check is existence rather than validity.
        mark_active(source=_source_origin(request), external=not boot_serving_https(),
                    confirmed=ticket_exists(data.get("ticket")))
        return JSONResponse(redeem_ticket(data.get("ticket"), _request_origin(request)),
                            headers={"Cache-Control": "no-store"})
    except HandoffSessionExpired as error:
        return JSONResponse({"error": str(error), "profile": error.profile, "route": error.route},
                            status_code=401, headers={"Cache-Control": "no-store"})
    except PermissionError as error:
        return JSONResponse({"error": str(error)}, status_code=401)
    except (ValueError, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
