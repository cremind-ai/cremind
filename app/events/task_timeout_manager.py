"""Deadline enforcement for skill-event and file-watcher EVENT TASKS.

A task exists because a conversation is blocked on an outcome. If the awaited
event simply never happens — the customer never replies, CI never reports, the
log file is never written — that conversation would wait forever. This manager
is the backstop: it flips an expired task to ``timed_out`` and delivers a
"never fired" result so the flow resumes and the agent can tell the user.

A plain periodic DB scan, deliberately:

- **restart-proof by construction** — the deadline lives in ``timeout_at``, so
  nothing has to be re-armed at boot and an overdue task fires on the first tick;
- **race-free** — expiry goes through the same ``active`` claim a real firing
  uses, so a task whose run is already in flight (``triggered``) can never be
  timed out from under it, and vice versa;
- **cheap** — these tables hold tens of rows, and the sweep only touches tasks.

Schedule tasks are never scanned: they fire at a known time, so "it never
happened" is not a state they can reach.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.utils.logger import logger


class TaskTimeoutManager:
    """Periodic sweep that expires overdue event tasks."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._stopping = False
        self._task = loop.create_task(self._run_loop(), name="task_timeout_manager")
        logger.info("TaskTimeoutManager: started")

    def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    # ── loop ────────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        from app.events.run_config import task_timeout_sweep_seconds

        while not self._stopping:
            try:
                await asyncio.sleep(task_timeout_sweep_seconds())
            except asyncio.CancelledError:
                return
            if self._stopping:
                return
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("TaskTimeoutManager: sweep failed")

    async def sweep_once(self) -> int:
        """Expire every task past its deadline. Returns how many were expired."""
        from app.events.event_task_delivery import deliver_timeout
        from app.storage import get_event_subscription_storage, get_file_watcher_storage

        expired = 0
        for source_kind, storage in (
            ("skill_event", get_event_subscription_storage()),
            ("file_watcher", get_file_watcher_storage()),
        ):
            try:
                due = storage.list_due_timeouts()
            except Exception:  # noqa: BLE001
                logger.exception(f"TaskTimeoutManager: failed to list due {source_kind} tasks")
                continue
            for sub in due:
                # The claim decides: a task that fired between the SELECT and
                # here is now 'triggered' and must be left alone.
                try:
                    if not storage.claim_task_timeout(sub["id"]):
                        continue
                except Exception:  # noqa: BLE001
                    logger.exception(f"TaskTimeoutManager: claim failed for {sub.get('id')}")
                    continue
                logger.info(
                    f"TaskTimeoutManager: {source_kind} task {sub['id']} timed out "
                    f"(deadline {sub.get('timeout_at')})"
                )
                try:
                    await deliver_timeout(source_kind, sub)
                    expired += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"TaskTimeoutManager: timeout delivery failed for {sub.get('id')}"
                    )
        return expired


_instance: Optional[TaskTimeoutManager] = None


def get_task_timeout_manager() -> TaskTimeoutManager:
    global _instance
    if _instance is None:
        _instance = TaskTimeoutManager()
    return _instance
