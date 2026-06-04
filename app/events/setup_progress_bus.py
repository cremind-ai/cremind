"""In-memory pub/sub bus for live Setup Wizard progress events.

Mirrors :class:`EmbeddingStateStreamBus` but carries opaque log entries
instead of state snapshots. Only one setup runs at a time (the wizard
serialises calls and the bootstrap window is single-tenant), so no
profile or session key is needed.

Each frame is a dict with at least ``step``, ``message``, ``level``,
and ``ts`` — the exact shape is decided by the publisher in
:mod:`app.api.config`.

Cross-thread safety: ``installer.install_features`` runs on a worker
thread and the deferred boot also touches threads. ``loop.call_soon_threadsafe``
is used to deliver into asyncio queues that belong to the main event
loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class SetupProgressBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subs.append((queue, loop))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(q, l) for (q, l) in self._subs if q is not queue]

    def publish(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for queue, loop in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, entry)
            except RuntimeError:
                # Loop is closed (subscriber went away mid-publish).
                # Drop silently — the disconnect handler will clean up.
                pass


_instance: SetupProgressBus | None = None


def get_setup_progress_bus() -> SetupProgressBus:
    global _instance
    if _instance is None:
        _instance = SetupProgressBus()
    return _instance


def publish_setup_event(payload: Dict[str, Any]) -> None:
    """Push a progress entry to every connected SSE subscriber.

    Safe to call from sync code on any thread (the bus dispatches via
    ``call_soon_threadsafe``). Failures are swallowed — a broken stream
    must never break the setup flow that triggered the publish.
    """
    try:
        get_setup_progress_bus().publish(payload)
    except Exception:  # noqa: BLE001
        from app.utils.logger import logger
        logger.debug("setup-progress bus publish failed", exc_info=True)
