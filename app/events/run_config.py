"""Global engine knobs for event runs, read from ``[event_runs]`` in settings.

``max_parallel_runs`` sizes one process-wide semaphore (a global cap; per-profile
overrides don't map onto it, so it is TOML-only). ``run_history_cap`` bounds the
per-rule run history kept in ``event_runs`` (older terminal runs pruned on
insert). Both are clamped to safe ranges.
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
