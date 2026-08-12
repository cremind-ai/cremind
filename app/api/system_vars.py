"""System-variables introspection endpoint.

Exposes the registry from :mod:`app.config.system_vars` over HTTP so the
`cremind` CLI can list the env vars Cremind injects into ``exec_shell``-spawned
subprocesses. Returns each variable's name, description, and the value
the server would inject for the caller's profile (resolved from the JWT).
The ``CREMIND_TOKEN`` value is the same JWT the caller used to authenticate,
so echoing it back is not a privacy escalation.
"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import verify_token
from app.config.system_vars import SYSTEM_VARS


async def list_system_vars(request: Request) -> JSONResponse:
    if not request.user.is_authenticated:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Invalid authorization header"}, status_code=401)
    # Full validation, not just a decode: ``CREMIND_TOKEN``'s resolver reads the
    # on-disk token file, so a revoked-token holder reaching this route would
    # otherwise be handed its own freshly-rotated replacement.
    payload = verify_token(auth_header.split("Bearer ", 1)[1])
    if payload is None:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    profile = payload.get("profile") or payload.get("sub") or ""

    return JSONResponse([
        {
            "name": spec.name,
            "description": spec.description,
            "value": spec.resolve(profile),
            "secret": spec.secret,
        }
        for spec in SYSTEM_VARS
    ])


def get_system_vars_routes() -> list[Route]:
    return [
        Route("/api/system-vars", list_system_vars, methods=["GET"]),
    ]
