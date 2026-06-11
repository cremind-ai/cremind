from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


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
