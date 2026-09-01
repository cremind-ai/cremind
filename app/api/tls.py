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

import ipaddress
import os

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


async def get_ca_pem(_request: Request) -> FileResponse | JSONResponse:
    """Serve ``<system dir>/tls/ca.pem`` as a download.

    Gated on the file existing rather than on ``SSL_MODE``: the precedence
    between an explicit certificate pair, ``auto``, and the modes that disable
    TLS lives in ``server._resolve_tls``, and duplicating it here would only
    drift. Serving a CA while TLS happens to be off is harmless — it is public
    material either way — and it keeps the download working for a device that
    is being set up before the server is restarted with TLS on.
    """
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
    device is another machine entirely. Under Electron the server never
    serves TLS at all, so there is nothing to trust.
    """
    from app.config.install_catalog import get_active_install_mode

    mode = (get_active_install_mode() or "").strip().lower()
    if mode in ("docker", "kubernetes"):
        return (
            "This server runs in a container, so it can only write the "
            "container's trust store — not the one your browser uses. "
            "Trust the CA on your own machine instead."
        )
    if os.environ.get("CREMIND_ELECTRON_PARENT") is not None:
        return "The desktop app never serves TLS; there is nothing to trust."
    return None


def local_trust_capabilities(request: Request) -> dict:
    """The ``local_trust`` sub-block of the capabilities ``tls`` payload.

    Computed per request on purpose: ``supported`` includes the loopback
    check, so a browser on another machine is never offered a button that
    would trust the CA on the wrong device.
    """
    ca_path = _ca_path()
    env_error = _trust_environment_error()
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
    - container/Kubernetes/Electron are refused — the store this process can
      reach is not the one the browser consults (``_trust_environment_error``);
    - the TCP peer must be loopback, i.e. the browser really is on this
      machine;
    - the body must echo the CA's SHA-256 fingerprint. That proves the caller
      has read it from the same-origin capabilities payload (a cross-site
      page cannot: reading requires CORS this server does not grant), and it
      pins the request to *this* CA — a stale page cannot trust a CA that has
      since been regenerated.

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
    ]
