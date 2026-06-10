from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.utils.request_origin import record_origin


class RequestOriginRecorder:
    """Record the browser's loopback origin (``Host`` header) on every backend
    HTTP request, so the Google OAuth redirect can track whatever local port the
    user reaches Cremind on (``kubectl port-forward <port>:80``) instead of a
    chart-fixed ``APP_URL``. Pure pass-through. See app.utils.request_origin."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            for key, value in scope.get("headers") or []:
                if key == b"host":
                    try:
                        record_origin(value.decode("latin-1"))
                    except Exception:  # noqa: BLE001 — never break a request over this
                        pass
                    break
        await self.app(scope, receive, send)


class A2AAuthGuard:
    """Middleware that requires authentication for A2A protocol endpoints."""

    # The A2A JSON-RPC endpoint
    PROTECTED_PATHS = {"/"}

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and scope["path"] in self.PROTECTED_PATHS:
            request = Request(scope)
            # Skip OPTIONS (CORS preflight)
            if request.method != "OPTIONS" and not request.user.is_authenticated:
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
