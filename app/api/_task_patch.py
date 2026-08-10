"""Shared PATCH handling for the event-task fields on a subscription.

``skill_event_subscriptions`` and ``file_watcher_subscriptions`` expose the same
task columns, so both APIs accept the same edits and must reject the same ones.
Keeping the rules here stops the two endpoints from drifting into different
answers for the same request.

Write side takes ``timeout_minutes`` (what a human or the model thinks in);
read side returns the stored ``timeout_at`` epoch. ``null`` clears the deadline.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from app.events.task_policy import TASK_TIMEOUT_MAX_MINUTES, TASK_TIMEOUT_MIN_MINUTES

#: Task lifecycle values that mean "this task is spent" — such a row is history
#: and only DELETE applies to it.
TERMINAL_TASK_STATUSES = ("completed", "cancelled", "timed_out")


def terminal_task_error(existing: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], int]]:
    """``(payload, status)`` when the row is a finished task, else ``None``."""
    if not existing.get("task"):
        return None
    status = existing.get("task_status")
    if status not in TERMINAL_TASK_STATUSES:
        return None
    return (
        {
            "error": "task_finished",
            "message": (
                f"This one-shot task is already {status}; finished tasks are "
                "read-only history. Delete it if you no longer want the record."
            ),
        },
        409,
    )


def apply_timeout_patch(
    body: Dict[str, Any], existing: Dict[str, Any], fields: Dict[str, Any],
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Fold ``timeout_minutes`` from ``body`` into ``fields``.

    Returns ``(payload, status)`` on a rejection, or ``None`` when there is
    nothing to do or the edit was accepted.
    """
    if "timeout_minutes" not in body:
        return None
    if not existing.get("task"):
        return (
            {
                "error": "not_a_task",
                "message": (
                    "timeout_minutes applies only to one-shot tasks; this is a "
                    "standing subscription, which has no deadline."
                ),
            },
            400,
        )
    raw = body.get("timeout_minutes")
    if raw is None:
        fields["timeout_at"] = None
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = -1
    if not (TASK_TIMEOUT_MIN_MINUTES <= minutes <= TASK_TIMEOUT_MAX_MINUTES):
        return (
            {
                "error": "invalid_timeout",
                "message": (
                    f"timeout_minutes must be between {TASK_TIMEOUT_MIN_MINUTES} "
                    f"and {TASK_TIMEOUT_MAX_MINUTES} (30 days), or null to clear."
                ),
            },
            400,
        )
    # Measured from now, like the tool path: "give it another 2 hours" is what a
    # user editing a deadline means, not "2 hours after it was registered".
    fields["timeout_at"] = time.time() + minutes * 60
    return None
