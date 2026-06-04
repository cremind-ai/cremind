"""In-memory pub/sub bus signalling Settings-page state changes.

Mirrors :class:`SkillEventsAdminStreamBus`: profile-keyed, threading-safe so
any async or sync caller can publish.

Wakeup-only — building the actual snapshot (tools, agents, llm-providers,
setup-status, skill-mode) requires async DB calls and registry walks, so
the snapshot lives in the SSE endpoint generator. Subscribers wake on each
tick and rebuild the snapshot before yielding the next frame.

Publishers: tool config update, agent enable/disable, agent register/
unregister, skill register, skill mode toggle, LLM provider mutation,
setup completion.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class SettingsStateStreamBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[str, List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]]] = {}

    def subscribe(self, profile: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subs.setdefault(profile, []).append((queue, loop))
        return queue

    def unsubscribe(self, profile: str, queue: asyncio.Queue) -> None:
        with self._lock:
            bucket = self._subs.get(profile)
            if not bucket:
                return
            self._subs[profile] = [(q, l) for (q, l) in bucket if q is not queue]
            if not self._subs[profile]:
                del self._subs[profile]

    def publish(self, profile: str, entry: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs.get(profile, ()))
        for queue, loop in subs:
            loop.call_soon_threadsafe(queue.put_nowait, entry)


_instance: SettingsStateStreamBus | None = None


def get_settings_state_stream_bus() -> SettingsStateStreamBus:
    global _instance
    if _instance is None:
        _instance = SettingsStateStreamBus()
    return _instance


def publish_settings_state_changed(profile: str | None) -> None:
    """Tell the settings-state SSE endpoint to rebuild and push a snapshot.

    Wakeup-only — subscribers rebuild the snapshot themselves.
    Safe to call from sync code (the bus dispatches across loops via
    ``call_soon_threadsafe``).
    """
    if not profile:
        return
    try:
        get_settings_state_stream_bus().publish(profile, {})
    except Exception:  # noqa: BLE001
        from app.utils.logger import logger
        logger.debug("settings-state bus publish failed", exc_info=True)
