"""Calendar & Schedule action subtools for the ``scheduler`` group.

These are the functions that make ``scheduler`` a *full* scheduler (not just a
parser) when the per-profile Calendar & Schedule feature is on: create / list /
cancel time-based Schedule Events. They are members of the ``scheduler`` builtin
group and are gated by :func:`app.tools.builtin.scheduler.get_prepare_tools` —
when the feature is OFF they are stripped from the child LLM's tool list, so the
agent only ever sees the always-on ``scheduler`` parser (today's behavior).

Flow: the reasoning agent first calls ``scheduler`` to normalize a time+schedule
expression (yielding ``dtstart`` and, for a recurrence, an RRULE), then calls
``schedule_create`` copying those normalized values verbatim — the LLM never
computes datetimes here, it only forwards what the parser already produced.

A created event is a Cremind Schedule Event managed by the ScheduleManager: it
fires the action in the conversation that created it (via the injected
``_context_id``), exactly like the other event types.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.utils.logger import logger

ACTION_TOOL_NAMES = frozenset({"schedule_create", "schedule_list", "schedule_cancel"})


def calendar_schedule_enabled(profile: Optional[str] = None) -> bool:
    """Convenience used by the gate / subtools to read the per-profile flag."""
    from app.calendar.feature import is_enabled
    return is_enabled(profile or "")


def _disabled_result() -> BuiltInToolResult:
    return BuiltInToolResult(structured_content={
        "ok": False,
        "error": "feature_disabled",
        "message": (
            "The Calendar & Schedule feature is turned off for this profile. "
            "Turn it on from the Calendar & Schedule page to create or manage "
            "scheduled events."
        ),
    })


async def _resolve_conversation_id(profile: str, context_id: Optional[str]) -> Optional[str]:
    from app.storage import get_conversation_storage
    from app.api.calendar import SCHEDULE_CONTEXT_ID
    conv_storage = get_conversation_storage()
    cid = context_id or SCHEDULE_CONTEXT_ID
    conv = await conv_storage.get_or_create_conversation(profile=profile, context_id=cid)
    return (conv or {}).get("id")


class ScheduleCreateTool(BuiltInTool):
    name: str = "schedule_create"
    description: str = (
        "Create a Calendar & Schedule event (a time-based Cremind event). Use "
        "AFTER calling `scheduler` to normalize the time: copy the parser's "
        "dtstart and (for recurrences) rrule verbatim. The event fires at its "
        "time(s): a reminder-only event raises a notification; otherwise the "
        "given action runs in this conversation. Open-ended recurrences are "
        "stored as a single advancing rule, never an infinite set."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short human-readable name for the event/reminder.",
            },
            "dtstart": {
                "type": "string",
                "description": (
                    "First occurrence as naive local ISO 'YYYY-MM-DDTHH:MM:SS' — "
                    "copy from the `scheduler` result (instant.datetime, "
                    "interval.start, or recurrence.dtstart). Never invent it."
                ),
            },
            "action": {
                "type": "string",
                "description": (
                    "The command to run when the event fires (e.g. 'summarize my "
                    "unread email'). Leave empty for a pure reminder."
                ),
            },
            "is_reminder_only": {
                "type": "boolean",
                "description": (
                    "True for a plain reminder (notification, no agent run). "
                    "Defaults to true when no action is given."
                ),
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Event length in minutes (for calendar display). Default 30.",
            },
            "end": {
                "type": "string",
                "description": (
                    "End as naive local ISO 'YYYY-MM-DDTHH:MM:SS' for a span/"
                    "multi-day event (e.g. a trip). Copy from the `scheduler` "
                    "interval.end. The span is computed from dtstart..end; omit "
                    "for a point-in-time event."
                ),
            },
            "all_day": {
                "type": "boolean",
                "description": (
                    "True for an all-day (date-only) event, e.g. a multi-day trip "
                    "with no specific time-of-day."
                ),
            },
            "rrule": {
                "type": "string",
                "description": (
                    "RFC 5545 RRULE value WITHOUT the 'RRULE:' prefix, for a "
                    "recurring event — copy from the `scheduler` recurrence.rrule. "
                    "Omit for a one-time event."
                ),
            },
            "recurrence_end_type": {
                "type": "string",
                "enum": ["never", "count", "until"],
                "description": "Copy from the `scheduler` recurrence_end.type.",
            },
            "recurrence_end_value": {
                "type": "string",
                "description": (
                    "Copy from the `scheduler` recurrence_end.value: an integer "
                    "count (as a string) or an ISO datetime for 'until'. Omit for "
                    "'never'."
                ),
            },
        },
        "required": ["title", "dtstart"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: str = (arguments.get("_profile") or "").strip()
        context_id: Optional[str] = arguments.get("_context_id")
        if not profile or not calendar_schedule_enabled(profile):
            return _disabled_result()

        dtstart = (arguments.get("dtstart") or "").strip()
        if not dtstart:
            return BuiltInToolResult(structured_content={
                "ok": False, "error": "missing_parameter",
                "message": "dtstart is required (copy it from the scheduler result).",
            })
        title = (arguments.get("title") or "").strip() or "Scheduled event"
        action = (arguments.get("action") or "").strip()
        is_reminder_only = bool(arguments.get("is_reminder_only", not action))
        rrule = (arguments.get("rrule") or "").strip() or None
        all_day = bool(arguments.get("all_day", False))

        # Duration: explicit minutes, else derived from an `end` (span / multi-day).
        duration_minutes = int(arguments.get("duration_minutes") or 0)
        end = (arguments.get("end") or "").strip()
        if not duration_minutes and end:
            from app.calendar import recurrence as _R
            try:
                mins = int((_R.parse_local(end) - _R.parse_local(dtstart)).total_seconds() // 60)
                if mins > 0:
                    duration_minutes = mins
            except Exception:  # noqa: BLE001
                pass
        if not duration_minutes:
            duration_minutes = 1440 if all_day else 30

        try:
            conversation_id = await _resolve_conversation_id(profile, context_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedule_create: conversation resolve failed")
            return BuiltInToolResult(structured_content={
                "ok": False, "error": "conversation_error", "message": str(exc),
            })
        if not conversation_id:
            return BuiltInToolResult(structured_content={
                "ok": False, "error": "conversation_error",
                "message": "Could not resolve the active conversation.",
            })

        from app.calendar.provider import get_calendar_provider
        provider = get_calendar_provider(profile)
        try:
            row = provider.create_event(
                profile=profile,
                conversation_id=conversation_id,
                title=title,
                action=action,
                is_reminder_only=is_reminder_only,
                source="agent",
                schedule_kind=("recurrence" if rrule else ("interval" if duration_minutes > 30 else "instant")),
                dtstart=dtstart,
                duration_minutes=duration_minutes,
                all_day=all_day,
                rrule=rrule,
                recurrence_end_type=arguments.get("recurrence_end_type"),
                recurrence_end_value=(
                    str(arguments["recurrence_end_value"])
                    if arguments.get("recurrence_end_value") not in (None, "")
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedule_create: provider.create_event failed")
            return BuiltInToolResult(structured_content={
                "ok": False, "error": "create_failed", "message": str(exc),
            })

        _publish_changed(profile)
        kind = "reminder" if is_reminder_only else "scheduled action"
        when = "recurring" if rrule else "one-time"
        return BuiltInToolResult(structured_content={
            "ok": True,
            "id": row["id"],
            "title": title,
            "schedule_kind": row["schedule_kind"],
            "dtstart": row["dtstart"],
            "rrule": rrule,
            "next_fire_at": row.get("next_fire_at"),
            "status": row.get("status"),
            "message": f"Created a {when} {kind} '{title}' starting {dtstart}.",
        })


class ScheduleListTool(BuiltInTool):
    name: str = "schedule_list"
    description: str = (
        "List this profile's Calendar & Schedule events (active and recently "
        "completed), with their next fire time and status. Use this to find an "
        "event's id before cancelling it."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "only_active": {
                "type": "boolean",
                "description": "If true, list only events that are still pending. Default true.",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: str = (arguments.get("_profile") or "").strip()
        if not profile or not calendar_schedule_enabled(profile):
            return _disabled_result()
        only_active = bool(arguments.get("only_active", True))
        from app.calendar.provider import get_calendar_provider
        rows = get_calendar_provider(profile).list_subscriptions(profile)
        events: List[Dict[str, Any]] = []
        for r in rows:
            if only_active and r.get("status") != "active":
                continue
            events.append({
                "id": r["id"],
                "title": r.get("title"),
                "action": r.get("action"),
                "is_reminder_only": r.get("is_reminder_only"),
                "schedule_kind": r.get("schedule_kind"),
                "dtstart": r.get("dtstart"),
                "rrule": r.get("rrule"),
                "status": r.get("status"),
                "next_fire_at": r.get("next_fire_at"),
            })
        return BuiltInToolResult(structured_content={"ok": True, "count": len(events), "events": events})


class ScheduleCancelTool(BuiltInTool):
    name: str = "schedule_cancel"
    description: str = (
        "Cancel a Calendar & Schedule event by its id so it stops firing. Call "
        "`schedule_list` first if you don't have the id."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "The id of the event to cancel."},
        },
        "required": ["event_id"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: str = (arguments.get("_profile") or "").strip()
        if not profile or not calendar_schedule_enabled(profile):
            return _disabled_result()
        event_id = (arguments.get("event_id") or "").strip()
        if not event_id:
            return BuiltInToolResult(structured_content={
                "ok": False, "error": "missing_parameter", "message": "event_id is required.",
            })
        from app.calendar.provider import get_calendar_provider
        provider = get_calendar_provider(profile)
        existing = [s for s in provider.list_subscriptions(profile) if s["id"] == event_id]
        if not existing:
            return BuiltInToolResult(structured_content={
                "ok": False, "error": "not_found",
                "message": f"No schedule event with id {event_id} for this profile.",
            })
        provider.set_status(event_id, "cancelled")
        _publish_changed(profile)
        return BuiltInToolResult(structured_content={
            "ok": True, "id": event_id,
            "message": f"Cancelled schedule event '{existing[0].get('title')}'.",
        })


def _publish_changed(profile: str) -> None:
    try:
        from app.api.calendar import publish_schedule_events_admin_changed
        publish_schedule_events_admin_changed(profile)
    except Exception:  # noqa: BLE001
        pass


def get_action_tools(config: dict) -> list[BuiltInTool]:
    return [ScheduleCreateTool(), ScheduleListTool(), ScheduleCancelTool()]
