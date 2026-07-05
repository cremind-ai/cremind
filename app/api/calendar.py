"""Calendar & Schedule API.

REST + SSE endpoints behind the *Calendar & Schedule* sidebar page and the
Events-page "Schedule Events" section:

- ``GET/PUT /api/calendar/settings``           — the per-profile feature switch
                                                  (+ Google connection status).
- ``GET  /api/calendar/events?from&to``         — occurrences in a window (recurrences
                                                  expanded on demand for the view).
- ``POST /api/calendar/events``                 — create a manual schedule event.
- ``PATCH/DELETE /api/calendar/events/{id}``    — edit / remove.
- ``GET  /api/schedule-events``                 — raw subscription rows (checklist /
                                                  Events page).
- ``GET  /api/schedule-events/admin/stream``    — SSE snapshot pushed on every change.
- ``DELETE /api/schedule-events/{id}``          — alias of the calendar delete.
- ``POST /api/schedule-events/{id}/status``     — pause / resume / cancel.

Manual events (created from the calendar UI, not by the agent) bind to the
per-profile dedicated ``__schedule__`` conversation; agent-created events bind to
their originating conversation (handled by the scheduler subtools, not here).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.calendar import feature as calendar_feature
from app.calendar.provider import get_calendar_provider
from app.events import get_schedule_manager
from app.events.schedule_events_admin_bus import get_schedule_events_admin_stream_bus
from app.storage import get_conversation_storage
from app.utils.logger import logger

# Reserved context_id for the per-profile conversation that manual calendar
# events run their actions in (and that reminder-only events nominally belong to).
SCHEDULE_CONTEXT_ID = "__schedule__"


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def publish_schedule_events_admin_changed(profile: Optional[str]) -> None:
    """Wake the Schedule-Events SSE subscribers so they rebuild + push a snapshot."""
    if not profile:
        return
    try:
        get_schedule_events_admin_stream_bus().publish(profile, {})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"schedule-events admin bus publish failed: {exc}")


async def _list_subscriptions_for_profile(profile: str) -> list[Dict[str, Any]]:
    rows = get_calendar_provider(profile).list_subscriptions(profile)
    if not rows:
        return []
    conv_storage = get_conversation_storage()
    titles: Dict[str, str] = {}
    for row in rows:
        cid = row["conversation_id"]
        if cid in titles:
            continue
        try:
            conv = await conv_storage.get_conversation(cid)
        except Exception:  # noqa: BLE001
            conv = None
        titles[cid] = (conv or {}).get("title") or "Untitled Chat"
    return [{**row, "conversation_title": titles.get(row["conversation_id"], "")} for row in rows]


async def build_schedule_events_admin_snapshot(profile: str) -> Dict[str, Any]:
    """Snapshot pushed on the multiplexed admin SSE (``schedule-events`` frame).

    Bundles the profile's schedule subscriptions + the feature flag so the
    Events-page section can replace its state in one go.
    """
    return {
        "subscriptions": await _list_subscriptions_for_profile(profile),
        "enabled": calendar_feature.is_enabled(profile),
    }


def _default_range() -> tuple[str, str]:
    """Current month +/- a week, as ISO strings — a sensible default window."""
    now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = (now.replace(day=1) - timedelta(days=7))
    end = now + timedelta(days=45)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def _norm_range(value: Optional[str], fallback: str) -> str:
    """Accept a date ('2026-06-21') or datetime; normalize to naive-local ISO."""
    if not value:
        return fallback
    v = value.strip()
    try:
        if len(v) == 10:  # date only
            return f"{v}T00:00:00"
        # tolerate trailing 'Z' / offsets by stripping to seconds
        dt = datetime.fromisoformat(v.replace("Z", ""))
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:  # noqa: BLE001
        return fallback


def get_calendar_routes(conversation_storage=None) -> list[Route]:

    # ── settings (the feature switch) ───────────────────────────────────

    async def handle_get_settings(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        provider = get_calendar_provider(profile)
        from app.calendar import google_auth
        g = google_auth.status(profile)
        return JSONResponse({
            "enabled": calendar_feature.is_enabled(profile),
            "google_connected": bool(g.get("connected")),
            "google_email": g.get("email"),
            "provider": getattr(provider, "name", "internal"),
        })

    async def handle_put_settings(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        enabled = bool(body.get("enabled"))
        calendar_feature.set_enabled(profile, enabled)
        # Arm or disarm this profile's existing schedule events to match.
        try:
            get_schedule_manager().set_profile_enabled(profile, enabled)
        except Exception:  # noqa: BLE001
            logger.exception("calendar settings: manager toggle failed")
        publish_schedule_events_admin_changed(profile)
        logger.info(f"[calendar] feature {'enabled' if enabled else 'disabled'} for profile={profile}")
        return JSONResponse({"enabled": enabled, "google_connected": False})

    # ── calendar occurrences (the grid) ─────────────────────────────────

    async def handle_list_occurrences(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        d_start, d_end = _default_range()
        rng_start = _norm_range(request.query_params.get("from"), d_start)
        rng_end = _norm_range(request.query_params.get("to"), d_end)
        events = get_calendar_provider(profile).list_occurrences(profile, rng_start, rng_end)
        return JSONResponse({"events": events, "from": rng_start, "to": rng_end})

    # ── raw subscriptions (checklist / Events page) ─────────────────────

    async def handle_list_subscriptions(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        subs = await _list_subscriptions_for_profile(profile)
        return JSONResponse({"subscriptions": subs})

    # ── create (manual) ─────────────────────────────────────────────────

    async def handle_create(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not calendar_feature.is_enabled(profile):
            return JSONResponse(
                {"error": "feature_disabled", "message": "Calendar & Schedule is turned off."},
                status_code=409,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        dtstart = (body.get("dtstart") or "").strip()
        if not dtstart:
            return JSONResponse(
                {"error": "missing_parameter", "message": "dtstart is required"},
                status_code=400,
            )
        title = (body.get("title") or "").strip() or "Untitled event"
        # Every scheduled event runs an action; default it to the title so an
        # event created with just a title still executes when it fires.
        action = (body.get("action") or "").strip() or title

        # Resolve the dedicated per-profile schedule conversation for manual events.
        conv_storage = conversation_storage or get_conversation_storage()
        try:
            conv = await conv_storage.get_or_create_conversation(
                profile=profile, context_id=SCHEDULE_CONTEXT_ID,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("calendar create: get_or_create_conversation failed")
            return JSONResponse({"error": "conversation_error", "message": str(exc)}, status_code=500)
        conversation_id = conv["id"]

        provider = get_calendar_provider(profile)
        try:
            row = provider.create_event(
                profile=profile,
                conversation_id=conversation_id,
                title=title,
                action=action,
                source="manual",
                schedule_kind=(body.get("schedule_kind") or ("recurrence" if body.get("rrule") else "instant")),
                dtstart=_norm_range(dtstart, dtstart),
                duration_minutes=int(body.get("duration_minutes") or 30),
                all_day=bool(body.get("all_day", False)),
                rrule=(body.get("rrule") or None),
                recurrence_end_type=body.get("recurrence_end_type"),
                recurrence_end_value=(str(body["recurrence_end_value"]) if body.get("recurrence_end_value") is not None else None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("calendar create: provider.create_event failed")
            return JSONResponse({"error": "create_failed", "message": str(exc)}, status_code=500)
        publish_schedule_events_admin_changed(profile)
        return JSONResponse({"ok": True, "event": row}, status_code=201)

    # ── update ───────────────────────────────────────────────────────────

    async def handle_update(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        event_id = request.path_params["id"]
        provider = get_calendar_provider(profile)
        existing = provider.list_subscriptions(profile)
        if not any(s["id"] == event_id for s in existing):
            return JSONResponse({"error": "Not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        allowed = {
            "title", "action", "all_day", "schedule_kind", "dtstart",
            "duration_minutes", "rrule", "recurrence_end_type", "recurrence_end_value",
        }
        fields = {k: v for k, v in body.items() if k in allowed}
        if "dtstart" in fields and fields["dtstart"]:
            fields["dtstart"] = _norm_range(str(fields["dtstart"]), str(fields["dtstart"]))
        row = provider.update_event(event_id, **fields)
        publish_schedule_events_admin_changed(profile)
        return JSONResponse({"ok": True, "event": row})

    # ── delete ───────────────────────────────────────────────────────────

    async def handle_delete(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        event_id = request.path_params["id"]
        provider = get_calendar_provider(profile)
        if not any(s["id"] == event_id for s in provider.list_subscriptions(profile)):
            return JSONResponse({"error": "Not found"}, status_code=404)
        # Cascade: delete this rule's run history + hidden run conversations
        # (usage survives) before removing the subscription row.
        from app.events.run_lifecycle import delete_runs_for_subscription, SCHEDULE
        await delete_runs_for_subscription(SCHEDULE, event_id, profile)
        provider.delete_event(event_id)
        publish_schedule_events_admin_changed(profile)
        return JSONResponse({"ok": True})

    # ── status (pause / resume / cancel) ─────────────────────────────────

    async def handle_status(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        event_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        status = (body.get("status") or "").strip()
        if status not in {"active", "paused", "cancelled"}:
            return JSONResponse(
                {"error": "invalid_status", "message": "status must be active|paused|cancelled"},
                status_code=400,
            )
        provider = get_calendar_provider(profile)
        if not any(s["id"] == event_id for s in provider.list_subscriptions(profile)):
            return JSONResponse({"error": "Not found"}, status_code=404)
        row = provider.set_status(event_id, status)
        publish_schedule_events_admin_changed(profile)
        return JSONResponse({"ok": True, "event": row})

    # ── Google Calendar connect / disconnect (Phase 2) ──────────────────

    async def handle_google_connect(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        from app.calendar import google_auth
        url = google_auth.build_authorize_url(profile)
        if not url:
            return JSONResponse(
                {
                    "error": "unavailable",
                    "message": (
                        "Google Calendar connect is not available: this server's "
                        "public URL or the cremind-connect Google client could not "
                        "be resolved. See the Calendar & Schedule setup docs."
                    ),
                },
                status_code=409,
            )
        return JSONResponse({"authorize_url": url})

    async def handle_google_disconnect(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        from app.calendar import google_auth
        google_auth.disconnect(profile)
        publish_schedule_events_admin_changed(profile)
        return JSONResponse({"ok": True, "google_connected": False})

    # Live updates are delivered on the multiplexed /api/admin/events-stream as
    # a ``schedule-events`` frame (see app/api/admin_stream.py) — no separate SSE
    # connection here.

    return [
        Route("/api/calendar/settings", handle_get_settings, methods=["GET"]),
        Route("/api/calendar/settings", handle_put_settings, methods=["PUT"]),
        Route("/api/calendar/events", handle_list_occurrences, methods=["GET"]),
        Route("/api/calendar/events", handle_create, methods=["POST"]),
        Route("/api/calendar/events/{id}", handle_update, methods=["PATCH"]),
        Route("/api/calendar/events/{id}", handle_delete, methods=["DELETE"]),
        Route("/api/calendar/google/connect", handle_google_connect, methods=["POST"]),
        Route("/api/calendar/google/disconnect", handle_google_disconnect, methods=["POST"]),
        Route("/api/schedule-events", handle_list_subscriptions, methods=["GET"]),
        Route("/api/schedule-events/{id}", handle_delete, methods=["DELETE"]),
        Route("/api/schedule-events/{id}/status", handle_status, methods=["POST"]),
    ]
