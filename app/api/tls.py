"""Local CA download — unauthenticated.

``CREMIND_SSL=auto`` and ``CREMIND_SSL=after-setup`` both sign the server's
certificate with a CA generated under ``<system dir>/tls/``. Browsers reject
that chain until the CA is in the *device's* trust store, so every device that
connects needs a copy of it once. This endpoint is how they get one — including
during after-setup's plain-HTTP wizard phase, where the CA already exists and
the Setup Wizard hands it over before the first HTTPS page is ever loaded.

It must be unauthenticated. The moment a user meets the warning is before they
have logged in — often before the Setup Wizard has even run — and the browser
showing the warning is exactly the client that cannot present a token. There is
nothing to protect either way: a CA certificate is public material, handed to
every TLS client during the handshake. Trusting it grants nothing except the
ability to verify certificates signed by a private key that never leaves the
server.

The filename is hardcoded on purpose. ``ca.key``, ``cert.pem`` and ``key.pem``
sit in the same directory, so a parameterised path here would be a private-key
disclosure one traversal bug away.
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.config.tls_auto import tls_dir


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


def get_tls_routes() -> list[Route]:
    return [Route("/ca.pem", get_ca_pem, methods=["GET"])]
