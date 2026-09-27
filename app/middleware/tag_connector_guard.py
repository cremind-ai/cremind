"""Keep Cremind Tag connector credentials on the connector API.

A request carrying ``Authorization: CremindTag …`` anywhere outside
``/api/tag-connector/v1/`` is answered 401 before authentication runs. The
JWT backend already ignores the scheme (it reads ``Bearer`` only), so such a
request would merely be anonymous — but "anonymous" still reaches the public
routes, and a connector secret has no business on any of them. Failing loudly
also tells a misconfigured companion (wrong base URL) what is wrong.

Pure ASGI, like the rest of :mod:`app.middleware`: it reads the path and one
header and never touches the body. Installed between ``ClientProtocolGuard``
and ``AuthenticationMiddleware``.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

CONNECTOR_PREFIX = "/api/tag-connector/v1/"
_SCHEME = b"cremindtag"
_MESSAGE = (
    "CremindTag credentials are accepted only by the connector API "
    f"({CONNECTOR_PREFIX}); use a session token here."
)


def _uses_tag_scheme(scope: Scope) -> bool:
    for key, value in scope.get("headers") or ():
        if key == b"authorization":
            return value.strip().split(b" ", 1)[0].lower() == _SCHEME
    return False


class TagConnectorGuard:
    """401 for the ``CremindTag`` scheme outside the connector prefix."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket") or not _uses_tag_scheme(scope):
            await self.app(scope, receive, send)
            return
        if scope.get("path", "").startswith(CONNECTOR_PREFIX):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"error": "tag_credential_not_accepted", "message": _MESSAGE, "detail": _MESSAGE},
            status_code=401,
        )
        await response(scope, receive, send)


__all__ = ["CONNECTOR_PREFIX", "TagConnectorGuard"]
