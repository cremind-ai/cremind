import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import verify_token
from app.config.settings import BaseConfig, get_user_working_directory
from app.config.working_dirs import is_default_location


async def get_me(request: Request) -> JSONResponse:
    """Return user information extracted from the JWT token."""
    if not request.user.is_authenticated:
        return JSONResponse(
            {"error": "Authentication required"},
            status_code=401,
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Invalid authorization header"}, status_code=401)

    payload = verify_token(auth_header.split("Bearer ", 1)[1])
    if payload is None:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    # The caller's OWN working directory (each profile has its own), and
    # whether it is the default ``<workspaces root>/<profile>`` rather than a
    # folder the admin chose. Tokens carry ``sub == profile``; ``null`` for
    # both when no profile can be named rather than someone else's folder.
    profile = (
        getattr(request.user, "username", "")
        or payload.get("profile")
        or payload.get("sub")
        or ""
    )
    user_working_dir = None
    user_working_dir_default = None
    if profile:
        try:
            wd = get_user_working_directory(profile)
        except ValueError:  # a name no folder can be made for (pseudo profile)
            wd = None
        if wd:
            user_working_dir = os.path.realpath(wd).replace(os.sep, "/")
            user_working_dir_default = is_default_location(profile, wd)
    return JSONResponse({
        "sub": payload.get("sub", ""),
        "profile": payload.get("profile", ""),
        "exp": payload.get("exp"),
        "iat": payload.get("iat"),
        "system_dir": os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR).replace(os.sep, "/"),
        "user_working_dir": user_working_dir,
        "user_working_dir_default": user_working_dir_default,
    })


def get_token_routes() -> list[Route]:
    return [
        Route("/api/me", get_me, methods=["GET"]),
    ]
