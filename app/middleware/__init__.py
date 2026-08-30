from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Connection-specific header fields, which RFC 9113 §8.2.2 forbids in HTTP/2.
# Same set as h2's own ``CONNECTION_HEADERS``.
_CONNECTION_HEADERS = frozenset(
    (b"connection", b"proxy-connection", b"keep-alive", b"transfer-encoding", b"upgrade")
)


class ConnectionHeaderFilter:
    """Drop connection-specific response headers so responses are HTTP/2-legal.

    Fifteen SSE endpoints set ``Connection: keep-alive`` explicitly, and
    ``sse_starlette`` (pulled in by a2a-sdk, so not ours to edit) sets it on
    every event stream unconditionally. All of them are illegal over HTTP/2.

    Today the h2 library happens to strip these on send, so nothing breaks
    without this — but that is a private implementation detail of a transitive
    dependency, and h2 also ships a validation mode that *raises* on them
    instead. Emitting legal headers in the first place is the app's job, not
    the transport's; doing it here rather than in fifteen response dicts also
    covers the third-party stream we cannot reach.

    Unconditional, because these headers are merely redundant on HTTP/1.1
    (persistent connections are the default there) — so there is nothing to
    sniff ``http_version`` for.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start" and message.get("headers"):
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message["headers"]
                        if name.lower() not in _CONNECTION_HEADERS
                    ],
                }
            await send(message)

        await self.app(scope, receive, _send)


class A2AAuthGuard:
    """Middleware that requires authentication for the A2A protocol endpoint."""

    # The A2A JSON-RPC endpoint
    PROTECTED_PATHS = {"/"}

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] in self.PROTECTED_PATHS:
            request = Request(scope)
            # Only the A2A JSON-RPC endpoint (POST /) is protected. The single
            # public origin also serves the SPA index at GET / (and OPTIONS is a
            # CORS preflight), so guard POST only and let everything else pass.
            if request.method == "POST" and not request.user.is_authenticated:
                response = JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": "Authentication required"},
                        "id": None,
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
