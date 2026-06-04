"""Shared auth helpers for API route handlers.

Three gates are exposed:

- :func:`require_auth` — caller must present a valid JWT.
- :func:`require_admin` — caller must be authenticated as the ``admin`` profile.
- :func:`require_auth_or_setup_mode` — caller must present a valid JWT *unless*
  first-run setup hasn't completed yet, in which case access is open. Used by
  the small set of read-only endpoints the setup wizard needs to bootstrap the
  admin profile (LLM providers, tools, agents).

Each helper returns either ``None`` (caller passes) or a 4xx ``JSONResponse``
that the route should return immediately.
"""

from starlette.requests import Request
from starlette.responses import JSONResponse


def require_auth(request: Request) -> JSONResponse | None:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def require_admin(request: Request) -> JSONResponse | None:
    unauth = require_auth(request)
    if unauth is not None:
        return unauth
    if getattr(request.user, "username", "") != "admin":
        return JSONResponse(
            {"error": "Admin profile required"}, status_code=403,
        )
    return None


def require_auth_or_setup_mode(request: Request, config_storage) -> JSONResponse | None:
    if config_storage is not None and not config_storage.is_setup_complete():
        return None
    return require_auth(request)
