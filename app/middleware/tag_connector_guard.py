"""Keep Cremind Tag credentials on the APIs they belong to.

- ``Authorization: CremindTag …`` (a worker/companion connector credential) is
  answered 401 anywhere outside ``/api/tag-connector/v1/``;
- ``Authorization: CremindSetup …`` (the one-session setup capability of a
  Cremind Connect launch link) is answered 401 anywhere outside
  ``/api/tag-setup/v1/``;
- ``Authorization: CremindHost …`` (a desktop hardware host's credential,
  scoped to that computer and one profile) is answered 401 anywhere outside
  ``/api/tag-host/v1/``.

All run before authentication. The JWT backend already ignores these schemes
(it reads ``Bearer`` only), so such a request would merely be anonymous — but
"anonymous" still reaches the public routes, and these secrets have no
business on any of them. Failing loudly also tells a misconfigured client
(wrong base URL) what is wrong.

Pure ASGI, like the rest of :mod:`app.middleware`: it reads the path and one
header and never touches the body. Installed between ``ClientProtocolGuard``
and ``AuthenticationMiddleware``.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

CONNECTOR_PREFIX = "/api/tag-connector/v1/"
SETUP_PREFIX = "/api/tag-setup/v1/"
HOST_PREFIX = "/api/tag-host/v1/"
_RULES = {
    b"cremindtag": (CONNECTOR_PREFIX, "tag_credential_not_accepted",
                    "CremindTag credentials are accepted only by the connector API "
                    f"({CONNECTOR_PREFIX}); use a session token here."),
    b"cremindsetup": (SETUP_PREFIX, "setup_credential_not_accepted",
                      "A Cremind Connect setup credential is accepted only by the setup API "
                      f"({SETUP_PREFIX})."),
    b"cremindhost": (HOST_PREFIX, "host_credential_not_accepted",
                     "A gateway computer's credential is accepted only by the host API "
                     f"({HOST_PREFIX})."),
}


def _scheme(scope: Scope) -> bytes | None:
    for key, value in scope.get("headers") or ():
        if key == b"authorization":
            return value.strip().split(b" ", 1)[0].lower()
    return None


class TagConnectorGuard:
    """401 for the Tag credential schemes outside their own API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        rule = _RULES.get(_scheme(scope) or b"") if kind in ("http", "websocket") else None
        if rule is None or scope.get("path", "").startswith(rule[0]):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"error": rule[1], "message": rule[2], "detail": rule[2]}, status_code=401)
        await response(scope, receive, send)


__all__ = ["CONNECTOR_PREFIX", "HOST_PREFIX", "SETUP_PREFIX", "TagConnectorGuard"]
