"""Process-wide vector embedding lifecycle state.

The setup wizard can flip Vector Embedding from off to on at runtime, and
the wizard's own UI needs to know when the model has finished loading
before letting the user enter the chat. Both the boot-time path
(``app.server.serve``) and the post-setup activation path
(``POST /api/config/embedding/initialize``) funnel through this singleton
so there's exactly one place that:

- holds the current ``LocalEmbeddings`` + ``VectorStore`` instances,
- exposes a status enum the UI polls,
- guards the agent against being used while loading.

Status transitions:

    DISABLED  -- embedding is off in config; nothing to do.
    INITIALIZING -- a load is in flight (boot or post-wizard).
    READY        -- model + vector store are live; agent can run.
    FAILED       -- last load attempt threw; ``error`` carries why.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.lib.embedding import LocalEmbeddings
    from app.vectorstores.base import VectorStore


class EmbeddingStatus(str, Enum):
    DISABLED = "disabled"
    INITIALIZING = "initializing"
    REBUILDING = "rebuilding"
    READY = "ready"
    FAILED = "failed"


# Statuses that mean "do not let the user interact with the agent yet."
BUSY_STATUSES = (EmbeddingStatus.INITIALIZING, EmbeddingStatus.REBUILDING)


def _publish_safe(snapshot: dict) -> None:
    """Best-effort push of a state snapshot to the SSE bus.

    Imported lazily to avoid a circular import (events package imports
    from app.utils which imports config). A failed publish must never
    propagate — that would corrupt the state mutation that triggered it.
    """
    try:
        from app.events.embedding_state_bus import publish_embedding_state_changed
    except Exception:  # noqa: BLE001
        return
    try:
        publish_embedding_state_changed(snapshot)
    except Exception:  # noqa: BLE001
        pass


class _EmbeddingState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: EmbeddingStatus = EmbeddingStatus.DISABLED
        self._error: Optional[str] = None
        self._phase: Optional[str] = None
        self._embedding: Optional["LocalEmbeddings"] = None
        self._vector_store: Optional["VectorStore"] = None

    @property
    def status(self) -> EmbeddingStatus:
        return self._status

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def phase(self) -> Optional[str]:
        return self._phase

    @property
    def embedding(self) -> Optional["LocalEmbeddings"]:
        return self._embedding

    @property
    def vector_store(self) -> Optional["VectorStore"]:
        return self._vector_store

    def is_ready(self) -> bool:
        return self._status is EmbeddingStatus.READY

    def is_busy(self) -> bool:
        return self._status in BUSY_STATUSES

    def is_initializing(self) -> bool:
        return self._status is EmbeddingStatus.INITIALIZING

    def _to_dict_locked(self) -> dict:
        """Snapshot the current state without re-acquiring the lock.

        Mutators call this while holding ``self._lock`` so the snapshot
        they hand to the SSE bus is consistent with the change they
        just made.
        """
        return {
            "status": self._status.value,
            "error": self._error,
            "phase": self._phase,
            "ready": self._status is EmbeddingStatus.READY,
            "busy": self._status in BUSY_STATUSES,
        }

    def to_dict(self) -> dict:
        with self._lock:
            return self._to_dict_locked()

    def set_phase(self, phase: Optional[str]) -> None:
        with self._lock:
            self._phase = phase
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)

    def mark_disabled(self) -> None:
        with self._lock:
            self._status = EmbeddingStatus.DISABLED
            self._error = None
            self._phase = None
            self._embedding = None
            self._vector_store = None
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)

    def mark_initializing(self) -> bool:
        """Atomically claim the right to initialize.

        Returns ``True`` if the caller acquired the slot (status moved to
        INITIALIZING) and should proceed with the load; ``False`` if a
        busy operation (initialize or rebuild) is already in flight.
        """
        with self._lock:
            if self._status in BUSY_STATUSES:
                return False
            self._status = EmbeddingStatus.INITIALIZING
            self._error = None
            self._phase = "loading_model"
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)
        return True

    def mark_rebuilding(self, phase: str = "rebuilding") -> bool:
        """Atomically transition into REBUILDING.

        Returns ``True`` on success; ``False`` if another busy operation
        is already running.
        """
        with self._lock:
            if self._status in BUSY_STATUSES:
                return False
            self._status = EmbeddingStatus.REBUILDING
            self._error = None
            self._phase = phase
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)
        return True

    def transition_to_rebuilding(self, *, phase: str = "rebuilding") -> None:
        """Force-transition INITIALIZING → REBUILDING.

        Caller must already own the BUSY slot (i.e. they previously
        called :meth:`mark_initializing` and got ``True`` back). Used by
        the apply pipeline to advance the visible phase from "loading
        model" to "rebuilding caches" without releasing and re-acquiring
        the slot.
        """
        with self._lock:
            self._status = EmbeddingStatus.REBUILDING
            self._phase = phase
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)

    def mark_ready(self, embedding, vector_store) -> None:
        with self._lock:
            self._status = EmbeddingStatus.READY
            self._error = None
            self._phase = None
            self._embedding = embedding
            self._vector_store = vector_store
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)

    def mark_failed(self, error: str) -> None:
        with self._lock:
            self._status = EmbeddingStatus.FAILED
            self._error = error
            self._phase = None
            self._embedding = None
            self._vector_store = None
            snapshot = self._to_dict_locked()
        _publish_safe(snapshot)


embedding_state = _EmbeddingState()


def initialize_embedding_subsystem():
    """Synchronously load the embedding model + vector store.

    Caller is responsible for arranging the right thread context — this
    blocks for as long as ``SentenceTransformer`` takes to download/load
    the chosen model. Returns ``(embedding, vector_store)`` on success;
    raises on failure (status is set to FAILED before re-raising).
    """
    from app.utils.logger import logger
    from app.lib.embedding import LocalEmbeddings
    from app.vectorstores import VectorStore, create_vector_store_client

    if not embedding_state.mark_initializing():
        raise RuntimeError("Embedding initialization already in progress.")

    try:
        embedding = LocalEmbeddings()
    except Exception as e:
        logger.exception("Failed to load embedding model")
        embedding_state.mark_failed(f"embedding model load failed: {e}")
        raise

    try:
        vector_store = VectorStore(client=create_vector_store_client())
    except Exception as e:
        logger.exception("Failed to connect vector store")
        embedding_state.mark_failed(f"vector store connection failed: {e}")
        raise

    embedding_state.mark_ready(embedding, vector_store)
    logger.info("Vector embedding subsystem ready.")
    return embedding, vector_store
