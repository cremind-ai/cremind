"""Periodic pruner for the temporary chat-upload folders.

A single long-running asyncio task wakes every ``uploads.tmp_prune_interval_minutes``
and removes per-conversation temp upload directories that have been idle past
``uploads.tmp_idle_minutes`` (see :mod:`app.utils.uploads_tmp`). The boot-time
full wipe is handled separately in ``app.server`` via ``wipe_all_on_startup``;
this manager only covers ongoing inactivity so the tree never accumulates junk
during a long-lived server process.

Mirrors the lifecycle shape of the other event managers (``start(loop)`` /
``stop()``) but uses the simple sleep-loop pattern rather than the
:class:`ScheduleManager` min-heap — there is nothing to schedule, just a fixed
interval to revisit.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.utils.logger import logger
from app.utils.uploads_tmp import idle_threshold_seconds, prune_idle, prune_interval_seconds


class UploadsCleanupManager:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = loop.create_task(self._run_loop(), name="uploads_cleanup")
        logger.info("UploadsCleanupManager: started")

    def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                # Sleep first so boot isn't slowed by a prune; the startup
                # wipe already cleared everything stale.
                await asyncio.sleep(prune_interval_seconds())
            except asyncio.CancelledError:
                break
            if self._stopping:
                break
            try:
                prune_idle(idle_threshold_seconds())
            except Exception:  # noqa: BLE001
                logger.exception("UploadsCleanupManager: prune cycle failed")


_manager: Optional[UploadsCleanupManager] = None


def get_uploads_cleanup_manager() -> UploadsCleanupManager:
    global _manager
    if _manager is None:
        _manager = UploadsCleanupManager()
    return _manager
