"""In-memory pub/sub bus for the global Vector Embedding lifecycle state.

Mirrors :class:`SettingsStateStreamBus` but with no profile key — the
embedding subsystem is a single process-wide resource (one model, one
vector-store connection), so every subscriber gets the same stream.

Each frame carries the full state snapshot. Unlike the settings stream
(which is wakeup-only and forces clients to refetch), the embedding
state is small enough to inline so the UI sees phase transitions
immediately.

Cross-thread safety: ``apply_embedding_config`` runs on a worker thread
spawned via ``run_in_executor``. ``loop.call_soon_threadsafe`` is used
to deliver into asyncio queues that belong to the main event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Tuple


class EmbeddingStateStreamBus:
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
                # Loop is closed (subscriber tab went away mid-publish).
                # Drop silently — the disconnect handler will clean up.
                pass


_instance: EmbeddingStateStreamBus | None = None


def get_embedding_state_stream_bus() -> EmbeddingStateStreamBus:
    global _instance
    if _instance is None:
        _instance = EmbeddingStateStreamBus()
    return _instance


def publish_embedding_state_changed(payload: Dict[str, Any]) -> None:
    """Push a state snapshot to every connected SSE subscriber.

    Safe to call from sync code on any thread (the bus dispatches via
    ``call_soon_threadsafe``). Failures are swallowed — a broken stream
    must never break the state mutation that triggered the publish.
    """
    try:
        get_embedding_state_stream_bus().publish(payload)
    except Exception:  # noqa: BLE001
        from app.utils.logger import logger
        logger.debug("embedding-state bus publish failed", exc_info=True)
