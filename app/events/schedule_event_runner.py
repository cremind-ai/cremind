"""How a fired schedule (time-based) event is worded for the agent.

Mirrors :mod:`app.events.file_watcher_runner`, but the payload comes from a
clock tick produced by :class:`app.events.schedule_manager.ScheduleManager`
rather than a watchdog event. Execution lives in
:mod:`app.events.run_dispatcher`, which runs the trigger in its own hidden
conversation and hands the result back to whichever conversation created the
schedule.
"""

from __future__ import annotations

from typing import Any, Dict


def _format_content(payload: Dict[str, Any]) -> str:
    """Build the trigger's content block (the detail lines shown in the UI bubble)."""
    lines = [
        f"title: {payload.get('title', '')}",
        f"fired_at: {payload.get('fired_at', '')}",
        f"schedule_kind: {payload.get('schedule_kind', '')}",
    ]
    if payload.get("rrule"):
        lines.append(f"rrule: {payload['rrule']}")
    if payload.get("next_fire_at_iso"):
        lines.append(f"next_occurrence: {payload['next_fire_at_iso']}")
    return "\n".join(lines)


def build_trigger_messages(action: str, payload: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(reasoning_query, bubble_content)`` for a fired schedule event.

    The reasoning ``Input:`` is just the action (the user's command, e.g.
    "tắt đèn hiên") — the schedule metadata is noise for execution. The UI
    bubble keeps the fenced detail block (rendered by stream_runner's
    ``_format_trigger_content``), so it is unchanged.
    """
    block = _format_content(payload)
    return action.strip(), f"---\n{block}\n---"

