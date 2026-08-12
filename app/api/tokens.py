import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import verify_token
from app.config.settings import BaseConfig, get_user_working_directory


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
    return JSONResponse({
        "sub": payload.get("sub", ""),
        "profile": payload.get("profile", ""),
        "exp": payload.get("exp"),
        "iat": payload.get("iat"),
        "system_dir": os.path.realpath(BaseConfig.CREMIND_SYSTEM_DIR).replace(os.sep, "/"),
        "user_working_dir": os.path.realpath(get_user_working_directory()).replace(os.sep, "/"),
    })


def get_token_routes() -> list[Route]:
    return [
        Route("/api/me", get_me, methods=["GET"]),
    ]
