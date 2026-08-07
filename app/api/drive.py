"""Per-file Google Drive access API.

Backs the Settings -> Google Drive page and the ``cremind drive`` CLI:

- ``GET  /api/drive/status``           — link state, granted scopes, staleness.
- ``GET  /api/drive/files``            — the files Cremind can actually reach.
- ``POST /api/drive/grants``           — start a Picker round; returns the URL.
- ``GET  /api/drive/grants/{state}``   — what that round has achieved so far.
- ``POST /api/drive/grants/complete``  — finish from a pasted redirect URL.
- ``DELETE /api/drive/grants/{state}`` — abandon a round.

Drive access itself lives with the gdrive skill (it owns the OAuth token); this
layer only reads that token and drives the Picker. There is no revoke endpoint
because Google offers no per-file revoke — the UI points at the user's Google
account connections page, which removes Cremind's access wholesale.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.drive import grant_flow, skill_token


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _file_ids(body: Dict[str, Any]) -> List[str]:
    raw = body.get("file_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return [
        ref
        for ref in (skill_token.parse_file_reference(str(v)) for v in raw)
        if ref
    ]


def get_drive_routes() -> List[Route]:
    async def handle_status(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        payload = skill_token.status(profile)
        payload["local_capture"] = grant_flow.capture_is_local()
        payload["capture_hint"] = grant_flow.capture_hint()
        payload["revoke_url"] = "https://myaccount.google.com/connections"
        # access_model / access_note come from skill_token so every client
        # describes the same account the same way.
        if payload["scopes_stale"]:
            payload["hint"] = (
                "This account is linked with the old whole-Drive scope, which Cremind "
                "no longer requests. Ask the agent to re-link the gdrive skill, then "
                "grant the files Cremind should reach."
            )
        return JSONResponse(payload)

    async def handle_list_files(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        page_token = request.query_params.get("page_token") or None
        try:
            size = int(request.query_params.get("page_size", "50"))
        except ValueError:
            size = 50
        try:
            return JSONResponse(
                skill_token.list_files(profile, page_token=page_token, page_size=size)
            )
        except skill_token.DriveTokenError as exc:
            return JSONResponse({"error": "unavailable", "message": str(exc)}, status_code=409)

    async def handle_start_grant(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        body = await _json_body(request)
        mime_types = body.get("mime_types") or None
        if isinstance(mime_types, str):
            mime_types = [m.strip() for m in mime_types.split(",") if m.strip()]
        try:
            return JSONResponse(
                grant_flow.start(
                    profile,
                    file_ids=_file_ids(body) or None,
                    allow_multiple=bool(body.get("allow_multiple", True)),
                    allow_folders=bool(body.get("allow_folders", True)),
                    mime_types=mime_types,
                )
            )
        except grant_flow.DriveGrantError as exc:
            return JSONResponse({"error": "unavailable", "message": str(exc)}, status_code=409)

    async def handle_grant_status(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        state = request.path_params["state"]
        return JSONResponse(grant_flow.poll_status(profile, state))

    async def handle_complete_grant(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        body = await _json_body(request)
        try:
            return JSONResponse(
                grant_flow.complete_from_redirect_url(profile, str(body.get("redirect_url", "")))
            )
        except grant_flow.DriveGrantError as exc:
            return JSONResponse({"error": "invalid", "message": str(exc)}, status_code=400)

    async def handle_cancel_grant(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        grant_flow.cancel(profile, request.path_params["state"])
        return JSONResponse({"ok": True})

    return [
        Route("/api/drive/status", handle_status, methods=["GET"]),
        Route("/api/drive/files", handle_list_files, methods=["GET"]),
        Route("/api/drive/grants", handle_start_grant, methods=["POST"]),
        Route("/api/drive/grants/complete", handle_complete_grant, methods=["POST"]),
        Route("/api/drive/grants/{state}", handle_grant_status, methods=["GET"]),
        Route("/api/drive/grants/{state}", handle_cancel_grant, methods=["DELETE"]),
    ]
