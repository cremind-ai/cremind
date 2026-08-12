"""Session-token status and rotation.

Backs the ``cremind auth`` CLI group. Two routes:

- ``GET  /api/auth/status``     — is my token still valid, and when does it die?
- ``POST /api/auth/regenerate`` — mint a fresh token and revoke every token
  previously issued to the profile.

Rotation *is* revocation: :func:`app.auth.rotate_profile_token` bumps the
profile's ``token_serial``, which every decode site compares against the token's
``tsr`` claim. Nothing here can help a caller whose token has already expired or
been revoked — that's what ``cremind auth regenerate --local`` is for, since it
talks to the database directly instead of authenticating first.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import is_admin, require_auth
from app.api.config import PROFILE_NAME_PATTERN
from app.auth import current_serial, rotate_profile_token, token_file_path, verify_token
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger


#: Upper bound on a requested token lifetime (one year). Without a cap, a
#: caller could mint an effectively immortal credential.
_MAX_EXPIRES_HOURS = 8760


def _caller_profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _bearer_payload(request: Request) -> dict | None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return verify_token(auth_header.split("Bearer ", 1)[1])


def _resolve_target(request: Request, requested: str) -> tuple[str | None, JSONResponse | None]:
    """Which profile this call acts on: your own, or another one if admin.

    Returns ``(name, None)`` or ``(None, error_response)``.
    """
    own = _caller_profile(request)
    if not requested or requested == own:
        return own, None
    if len(requested) > 64 or not PROFILE_NAME_PATTERN.match(requested):
        return None, JSONResponse(
            {"error": "invalid_profile", "message": f"Invalid profile name: {requested!r}"},
            status_code=400,
        )
    if not is_admin(request):
        return None, JSONResponse(
            {
                "error": "admin_required",
                "message": (
                    f"Only the admin profile can rotate another profile's token "
                    f"(you are '{own}')."
                ),
            },
            status_code=403,
        )
    return requested, None


async def handle_auth_status(request: Request) -> JSONResponse:
    """Report the presented token's serial against the profile's current one."""
    unauth = require_auth(request)
    if unauth is not None:
        return unauth

    payload = _bearer_payload(request)
    if payload is None:
        # Unreachable in practice — the middleware already validated this token
        # to populate request.user — but keeps the handler honest on its own.
        return JSONResponse({"error": "invalid_token", "message": "Invalid token"}, status_code=401)

    requested = request.query_params.get("profile", "") or ""
    profile, err = _resolve_target(request, requested)
    if err is not None:
        return err

    stored = await asyncio.to_thread(current_serial, profile)
    token_serial = int(payload.get("tsr", 0) or 0)
    own = _caller_profile(request)
    try:
        path = str(token_file_path(profile))
    except ValueError:
        path = ""

    return JSONResponse({
        "profile": profile,
        "sub": payload.get("sub", ""),
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
        # The serial the *presented* token carries. Only meaningful when the
        # caller is asking about its own profile; an admin inspecting someone
        # else is holding its own token, not theirs.
        "token_serial": token_serial if profile == own else None,
        "current_serial": stored,
        "valid": profile != own or token_serial == stored,
        "token_file": path,
    })


async def handle_auth_regenerate(
    request: Request, conversation_storage: ConversationStorage
) -> JSONResponse:
    """Rotate a profile's token, revoking every token issued to it before."""
    unauth = require_auth(request)
    if unauth is not None:
        return unauth

    body: dict = {}
    raw = await request.body()
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": "invalid_json", "message": "Request body must be JSON."},
                status_code=400,
            )
        if isinstance(parsed, dict):
            body = parsed

    profile, err = _resolve_target(request, str(body.get("profile") or ""))
    if err is not None:
        return err

    hours = None
    if body.get("expires_hours") is not None:
        try:
            hours = int(body["expires_hours"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "invalid_expires_hours", "message": "'expires_hours' must be an integer."},
                status_code=400,
            )
        if not 1 <= hours <= _MAX_EXPIRES_HOURS:
            return JSONResponse(
                {
                    "error": "invalid_expires_hours",
                    "message": f"'expires_hours' must be between 1 and {_MAX_EXPIRES_HOURS}.",
                },
                status_code=400,
            )

    if not await conversation_storage.profile_exists(profile):
        return JSONResponse(
            {"error": "profile_not_found", "message": f"Profile '{profile}' not found."},
            status_code=404,
        )

    try:
        # Off the event loop: unlike the cached serial reads on the auth path,
        # this is a write transaction plus a file write.
        result = await asyncio.to_thread(rotate_profile_token, profile, hours=hours)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[auth] token rotation failed for {profile!r}: {e}")
        return JSONResponse(
            {"error": "rotation_failed", "message": f"Could not rotate the token: {e}"},
            status_code=500,
        )

    logger.info(
        f"[auth] token rotated profile={profile} serial={result['serial']} "
        f"by={_caller_profile(request)}"
    )
    return JSONResponse(result)


def get_auth_routes(conversation_storage: ConversationStorage) -> list[Route]:
    async def _regenerate(request: Request) -> JSONResponse:
        return await handle_auth_regenerate(request, conversation_storage)

    return [
        Route("/api/auth/status", handle_auth_status, methods=["GET"]),
        Route("/api/auth/regenerate", _regenerate, methods=["POST"]),
    ]
