"""Google Suite account links — ``/api/google/*``.

Backs Settings -> GSuite, the ``cremind google`` CLI, and the Unlink
controls on the Drive and Calendar pages:

- ``GET  /api/google/accounts``                 — what each Google skill is linked to.
- ``POST /api/google/accounts/{skill}/unlink``  — revoke and wipe one link.
- ``POST /api/google/unlink-all``               — the same for every Google skill.

*Linking* belongs to the skills (each owns its OAuth token and runs the consent
flow in chat); this layer is the other half. The work itself lives in
:mod:`app.google.unlink`, which documents why it runs in-process and why the
teardown order is what it is.

Two response-shape decisions are load-bearing:

- **A failed revoke is still HTTP 200.** The local credential is gone, which is the
  half that matters for safety; the failure travels in ``revoked`` /
  ``revoke_error`` / ``message``. The one hard failure is a credential file that
  survived the wipe (``wipe_failed``), because a usable token is still on disk.
- **Unlinking something that is not linked is HTTP 200.** The UI can double-fire,
  and ``unlink-all`` on a profile that linked two of five skills would otherwise
  report three failures.

Prose always travels in ``message``: the CLI's error handling keeps only the
machine ``error`` code (see ``app/cli/client/_base.py``), so anything a human has
to read has to be there for the command to dig out.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.google import registry, unlink as engine
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an absent or malformed body means "defaults"
        return {}
    return body if isinstance(body, dict) else {}


def _status_for(result: Dict[str, Any]) -> int:
    """200 unless a credential file survived the wipe."""
    return 500 if result.get("still_linked") else 200


def get_google_routes() -> List[Route]:
    async def handle_accounts(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        return JSONResponse(engine.inventory(profile))

    async def handle_unlink(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        name = request.path_params["skill"]
        spec = registry.by_name(name)
        if spec is None:
            return JSONResponse(
                {
                    "error": "unsupported_skill",
                    "message": (
                        f"{name!r} is not a Google Suite skill. Choose one of: "
                        + ", ".join(sorted(registry.names()))
                        + "."
                    ),
                },
                status_code=400,
            )
        if engine.skill_dir(profile, spec) is None:
            return JSONResponse(
                {
                    "error": "skill_not_installed",
                    "message": (
                        f"The {spec.dir_name} skill is not installed for profile "
                        f"{profile!r}, so it holds no Google link."
                    ),
                },
                status_code=404,
            )

        body = await _json_body(request)
        result = await engine.unlink_skill(
            profile,
            spec,
            revoke=bool(body.get("revoke", True)),
            stop_watch=bool(body.get("stop_watch", True)),
            force_revoke=bool(body.get("force_revoke", False)),
        )
        _publish(profile, [spec.dir_name])
        if result.get("still_linked"):
            result = {**result, "error": "wipe_failed"}
        return JSONResponse(result, status_code=_status_for(result))

    async def handle_unlink_all(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        body = await _json_body(request)
        out = await engine.unlink_all(profile, revoke=bool(body.get("revoke", True)))
        _publish(profile, [row["skill"] for row in out.get("results", [])])
        if out.get("failed"):
            out = {**out, "error": "wipe_failed"}
            return JSONResponse(out, status_code=500)
        return JSONResponse(out)

    return [
        Route("/api/google/accounts", handle_accounts, methods=["GET"]),
        Route("/api/google/accounts/{skill}/unlink", handle_unlink, methods=["POST"]),
        Route("/api/google/unlink-all", handle_unlink_all, methods=["POST"]),
    ]


def _publish(profile: str, touched: List[str]) -> None:
    """Wake the settings page, and the calendar page when gcalendar moved.

    The settings stream is wakeup-only, so clients refetch — which is what makes a
    CLI unlink show up in an already-open browser tab.
    """
    try:
        from app.events.settings_state_bus import publish_settings_state_changed

        publish_settings_state_changed(profile)
    except Exception as exc:  # noqa: BLE001 - never fail a completed unlink on this
        logger.debug(f"[google] settings publish failed for {profile}: {exc}")
    if "gcalendar" not in touched:
        return
    try:
        from app.api.calendar import publish_schedule_events_admin_changed

        publish_schedule_events_admin_changed(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[google] calendar publish failed for {profile}: {exc}")
