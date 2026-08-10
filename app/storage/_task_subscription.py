"""Shared event-task claim helpers for the two "waits for something" families.

``skill_event_subscriptions`` and ``file_watcher_subscriptions`` carry an
identical event-task column set (``task``, ``task_status``, ``timeout_at``,
``completed_at``), so their claim/terminate logic lives here once instead of
being mirrored — the two copies would otherwise have to stay in lockstep
forever, and a drift between them is a double-fire bug.

Schedules are deliberately NOT included: their existing ``status`` column
already carries the lifecycle and :class:`ScheduleEventSubscriptionStorage`
claims one-shots with :meth:`claim_one_shot`.

The state machine (``task_status``, NULL for standing subscriptions):

    active ──claim_task_fire()───────► triggered ──set_task_status()──► completed
      │                                    │                          └► cancelled
      │                                    └──revert_task_claim()──► active
      └──claim_task_timeout()────► timed_out

Every transition is a single conditional ``UPDATE ... WHERE task_status = <old>``
whose rowcount is the answer. That is what makes a task fire exactly once when
two triggers land at the same moment, and what makes the timeout sweep a no-op
against a task whose run is already in flight.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text


class TaskSubscriptionMixin:
    """Event-task claim/terminate helpers. Requires ``_TASK_TABLE`` + ``_engine``."""

    #: Table this mixin operates on. Set by the concrete storage class.
    _TASK_TABLE: str = ""

    def claim_task_fire(self, id: str) -> bool:
        """Consume a task's single firing. ``True`` only for the winning caller.

        A losing caller (a second trigger for the same rule, or a race with the
        timeout sweep) must drop its trigger: the task has already been spent.
        """
        return self._claim(id, from_status="active", to_status="triggered")

    def claim_task_timeout(self, id: str) -> bool:
        """Consume a task by deadline. ``False`` once it has fired (or timed out)."""
        return self._claim(id, from_status="active", to_status="timed_out", stamp=True)

    def revert_task_claim(self, id: str) -> bool:
        """Re-arm a claimed task whose run could not be created. Best-effort."""
        return self._claim(id, from_status="triggered", to_status="active")

    def set_task_status(self, id: str, status: str) -> bool:
        """Terminate a claimed task (``completed`` after delivery, or ``cancelled``)."""
        return self._claim(id, from_status="triggered", to_status=status, stamp=True)

    def list_due_timeouts(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Armed tasks whose deadline has passed.

        Paused tasks are included on purpose: the timeout is a deadline on the
        answer the origin conversation is waiting for, not on the watcher, so a
        paused task still has to report back rather than hang the flow forever.
        """
        cutoff = time.time() if now is None else float(now)
        with self._engine.connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                text(
                    f"SELECT * FROM {self._TASK_TABLE} "  # noqa: S608 - class-constant table
                    "WHERE task = :task AND task_status = 'active' "
                    "AND timeout_at IS NOT NULL AND timeout_at <= :cutoff "
                    "ORDER BY timeout_at ASC"
                ),
                {"task": True, "cutoff": cutoff},
            ).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]  # type: ignore[attr-defined]

    def list_active_tasks(self, profile: Optional[str] = None) -> List[Dict[str, Any]]:
        """Armed (not yet fired) tasks, newest first — for listings and tests."""
        sql = (
            f"SELECT * FROM {self._TASK_TABLE} "  # noqa: S608 - class-constant table
            "WHERE task = :task AND task_status = 'active'"
        )
        params: Dict[str, Any] = {"task": True}
        if profile:
            sql += " AND profile = :profile"
            params["profile"] = profile
        sql += " ORDER BY created_at DESC"
        with self._engine.connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(text(sql), params).mappings().fetchall()
            return [self._row_to_dict(r) for r in rows]  # type: ignore[attr-defined]

    # ── internals ───────────────────────────────────────────────────────────

    def _claim(
        self, id: str, *, from_status: str, to_status: str, stamp: bool = False,
    ) -> bool:
        assignments = "task_status = :to_status"
        params: Dict[str, Any] = {
            "id": id, "to_status": to_status, "from_status": from_status, "task": True,
        }
        if stamp:
            assignments += ", completed_at = :now"
            params["now"] = time.time()
        with self._engine.begin() as conn:  # type: ignore[attr-defined]
            cur = conn.execute(
                text(
                    f"UPDATE {self._TASK_TABLE} SET {assignments} "  # noqa: S608
                    "WHERE id = :id AND task = :task AND task_status = :from_status"
                ),
                params,
            )
            return (cur.rowcount or 0) > 0
