"""What counts as an EVENT TASK, where a result may be reported, and timeouts.

**Reporting back is universal.** Every rule registered from a real conversation
— skill event, file watcher or schedule; one-shot or standing — hands each run's
result back to that conversation (see :mod:`app.events.event_task_delivery`).
:func:`is_deliverable_origin` is the single predicate that says which
conversations can receive one; the run dispatcher consults it once per fire.

**``task`` means ONE-SHOT**, and nothing else: wait for the NEXT matching
occurrence only, run once, report, then terminate — optionally giving up at a
deadline (:func:`resolve_task_timeout`). It is the shape behind "do X, wait for
the outcome, then do Y". A standing rule keeps firing and keeps reporting. The
predicates here decide which registration calls create the one-shot shape.

It is deliberately dependency-free (stdlib only): the reasoning agent imports it
at dispatch time, the three registration tools import it while validating, the
run dispatcher imports it at fire time, and tests import it directly. Keeping
the predicates here is what stops "the agent was allowed to register this" from
drifting away from "this actually comes back".
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

# Timeout bounds for skill/file-watcher tasks (schedules fire at a known time,
# so they never carry one). Minutes, because that is what the model writes.
TASK_TIMEOUT_MIN_MINUTES = 1
TASK_TIMEOUT_MAX_MINUTES = 43200        # 30 days
TASK_TIMEOUT_DEFAULT_MINUTES = 10080    # 7 days

#: How many ONE-SHOT tasks one flow may chain before the agent must stop and
#: hand back to the user. Only a one-shot continuation carries the depth on its
#: trigger payload (a recurring rule's result mints depth 0 — it is a fresh
#: report, not another hop in a wait chain); without a cap an email ping-pong
#: could self-perpetuate. Best-effort: nothing persists the depth on the rule.
MAX_TASK_CHAIN_DEPTH = 10

#: Registration leaves that become task registrations when their args say so.
_SCHEDULE_LEAF = ("scheduler", "schedule_create")
_WATCHER_LEAF = ("system_file", "register_file_watcher")

#: Conversations that exist only to own rules nobody reads: the calendar UI's
#: per-profile schedule host and the blueprint import host. The ``__`` prefix is
#: the reserved-context convention (``app.api.calendar.SCHEDULE_CONTEXT_ID``,
#: ``app.blueprint.apply._SKILL_EVENTS_CONTEXT_ID``).
_RESERVED_CONTEXT_PREFIX = "__"


def is_deliverable_origin(
    kind: Optional[str], context_id: Optional[str],
) -> bool:
    """Whether a run's result may be reported into this conversation.

    Pure: the caller passes the conversation row's ``kind`` and ``context_id``,
    and must treat a MISSING row (no conversation at all) as not deliverable —
    this function never touches storage.

    Two kinds of origin are refused. A hidden ``event_run`` conversation is an
    automation's own scratch space: reporting into it would let an automation
    feed itself. A reserved host (``__schedule__``, ``__skill_events__``) has no
    reader, so a rule bound to one stays notification-only exactly as before.

    Everything else is deliverable, including the two room shapes — a group-chat
    seat (``group:<gid>:<profile>``) and a platform group
    (``channel_group:<gid>``) — whose turns post their answer to the room.
    """
    if (kind or "chat") == "event_run":
        return False
    if str(context_id or "").startswith(_RESERVED_CONTEXT_PREFIX):
        return False
    return True


def resolve_task_timeout(
    raw: Any, *, task: bool,
) -> Tuple[Optional[float], Optional[str]]:
    """Turn a model-supplied ``timeout_minutes`` into an epoch deadline.

    Returns ``(timeout_at, error)`` — ``error`` is a model-facing correction and
    means nothing was registered. A task with no explicit timeout gets
    :data:`TASK_TIMEOUT_DEFAULT_MINUTES` so a flow can never hang forever.
    """
    if raw is None or raw == "":
        if not task:
            return None, None
        return time.time() + TASK_TIMEOUT_DEFAULT_MINUTES * 60, None

    if not task:
        return None, (
            "timeout_minutes only applies to a one-shot task. Either add "
            "task: true (if you want the outcome delivered back to this "
            "conversation) or drop timeout_minutes. Nothing was registered."
        )

    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        minutes = -1
    if not (TASK_TIMEOUT_MIN_MINUTES <= minutes <= TASK_TIMEOUT_MAX_MINUTES):
        return None, (
            f"timeout_minutes must be a whole number of minutes between "
            f"{TASK_TIMEOUT_MIN_MINUTES} and {TASK_TIMEOUT_MAX_MINUTES} "
            f"(30 days); got {raw!r}. Nothing was registered."
        )
    return time.time() + minutes * 60, None


def format_timeout_clause(timeout_at: Optional[float], raw: Any = None) -> str:
    """A short "(giving up …)" clause for a task's confirmation text."""
    if timeout_at is None:
        return ""
    if raw in (None, ""):
        return f" (timing out after {TASK_TIMEOUT_DEFAULT_MINUTES // 1440} days)"
    try:
        from datetime import datetime

        when = datetime.fromtimestamp(float(timeout_at)).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return f" (giving up after {raw} minutes)"
    return f" (giving up after {raw} minutes, around {when})"


def is_task_subscribe_args(subscribe: Optional[Dict[str, Any]]) -> bool:
    """True when a skill ``subscribe`` block asks for a one-shot task."""
    return bool((subscribe or {}).get("task"))


def is_task_watcher_args(args: Optional[Dict[str, Any]]) -> bool:
    """True when a ``register_file_watcher`` call asks for a one-shot task."""
    return bool((args or {}).get("task"))


def schedule_kind_for(
    *, rrule: Optional[str], all_day: bool, duration_minutes: int,
) -> str:
    """The ``schedule_kind`` a schedule row is stored under, from RESOLVED values.

    ``instant`` is the only kind that can be an EVENT TASK: a longer or all-day
    one-shot is a calendar block (a trip, a leave day), not an outcome anyone is
    waiting on.
    """
    if rrule:
        return "recurrence"
    if all_day:
        return "interval"
    return "interval" if int(duration_minutes or 0) > 30 else "instant"


def is_task_schedule_args(args: Optional[Dict[str, Any]]) -> bool:
    """Conservative "will this ``schedule_create`` call create a task?" check.

    Used at DISPATCH time, where only the raw model arguments are available (the
    tool has not normalized ``end`` into a duration yet). It is deliberately
    stricter than :func:`schedule_kind_for`: everything it approves really does
    become a task, so a turn that is only allowed to register tasks can never
    slip a non-returning event through. The reverse (a borderline call refused
    on a continuation turn) is a harmless retry.
    """
    args = args or {}
    if args.get("rrule") or args.get("all_day") or args.get("end"):
        return False
    try:
        duration = int(args.get("duration_minutes") or 0)
    except (TypeError, ValueError):
        duration = 0
    return duration <= 30


def is_task_registration(
    tool_id: str, leaf_name: str, args: Optional[Dict[str, Any]],
) -> bool:
    """True when this leaf call registers a one-shot task (not a standing rule).

    Used by the reasoning agent to decide, at dispatch time, whether a turn that
    was itself started by an event-task result may register what it is asking
    for. One-shot tasks chain (that is the point — wait, continue, wait again);
    standing automations must not, or they would re-register on every result.
    """
    entry = (tool_id, leaf_name)
    if entry == _WATCHER_LEAF:
        return is_task_watcher_args(args)
    if entry == _SCHEDULE_LEAF:
        return is_task_schedule_args(args)
    return False
