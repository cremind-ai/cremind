"""Global engine knobs for event runs, read from ``[event_runs]`` in settings.

``max_parallel_runs`` sizes one process-wide semaphore (a global cap; per-profile
overrides don't map onto it, so it is TOML-only). ``run_history_cap`` bounds the
per-rule run history kept in ``event_runs`` (older terminal runs pruned on
insert). ``task_timeout_sweep_seconds`` paces the event-task deadline sweep.
``max_results_per_delivery`` bounds how many of a standing rule's results one
conversation receives in a single continuation turn, and
``undelivered_max_age_hours`` how long an undelivered one stays worth reporting
— together they stop a busy rule (or a long outage) from dumping a backlog into
a chat, a room or a platform group. Both bound STANDING results only: a one-shot
task was explicitly awaited, so it is always delivered. All are clamped to safe
ranges.
"""

from __future__ import annotations

from app.config.settings import _dynaconf_get


def max_parallel_runs() -> int:
    try:
        v = int(_dynaconf_get("event_runs.max_parallel_runs", 5))
    except (TypeError, ValueError):
        v = 5
    return max(1, min(v, 64))


def run_history_cap() -> int:
    try:
        v = int(_dynaconf_get("event_runs.run_history_cap", 50))
    except (TypeError, ValueError):
        v = 50
    return max(1, min(v, 500))


def task_timeout_sweep_seconds() -> int:
    """How often to check for expired event tasks.

    Seconds-level precision is ample: task timeouts are minutes-to-days, and the
    deadline is persisted, so the only cost of a slow tick is a slightly late
    "it never fired" message.
    """
    try:
        v = int(_dynaconf_get("event_runs.task_timeout_sweep_seconds", 30))
    except (TypeError, ValueError):
        v = 30
    return max(5, min(v, 600))


def max_results_per_delivery() -> int:
    """How many STANDING results one conversation receives in one turn.

    A rule that fires faster than its conversation answers builds a backlog;
    delivering all of it at once would blow the context and read as spam. The
    newest N are reported and the rest are closed out as stale (they stay in the
    run history, which is where a user goes looking for them). One-shot task
    results are exempt — each was explicitly awaited by a flow.
    """
    try:
        v = int(_dynaconf_get("event_runs.max_results_per_delivery", 5))
    except (TypeError, ValueError):
        v = 5
    return max(1, min(v, 20))


def undelivered_max_age_hours() -> int:
    """How long an undelivered STANDING result is still worth reporting.

    Bounds the boot sweep after an outage: a week-old "here is today's news" is
    noise, and injecting a backlog of them into a room or a platform group is
    worse. ``0`` disables the bound. One-shot task results are exempt — their
    ``timeout_minutes`` is the deadline that governs them.
    """
    try:
        v = int(_dynaconf_get("event_runs.undelivered_max_age_hours", 72))
    except (TypeError, ValueError):
        v = 72
    return max(0, min(v, 8760))
