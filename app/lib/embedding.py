"""In-process embedding wrapper.

Replaces the gRPC client that previously talked to the standalone
``embedding_grpc`` service. Loads a sentence-transformers model directly
into the Cremind process and exposes the same ``embed_documents`` /
``embed_query`` interface so all existing call sites continue to work.

Satisfies the ``EmbeddingProvider`` protocol declared in
``app/vectorstores/base.py``.

**One lock around every model call.** The single instance is shared by chat
(documentation search, memory), the documents sync and User Document Search's
background indexer, which run on different threads. A Hugging Face *fast*
tokenizer is a Rust object that is not safe to use from two threads at once
(it fails with ``RuntimeError: Already borrowed``), so every call into the
provider holds ``_lock``.

**Interactive callers go first.** The userdocs indexer embeds in batches of up
to a few hundred milliseconds. A search query (``embed_query`` /
``embed_search_query``) registers itself in ``_interactive_waiting`` before it
queues for the lock, and ``embed_passages`` checks that counter before every
batch, so a query waits for at most the batch already running, never for the
whole backlog. Batch size adapts so one batch stays near ``_TARGET_BATCH_S``.

**Prefixes only on the new path.** ``embed_documents`` / ``embed_query`` keep
their exact historical outputs (no prompt prefix): the documentation, memory
and tool collections were built that way. ``embed_passages`` /
``embed_search_query`` add the provider's trained prompts; see
:mod:`app.userdocs.embed_prompts`.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional


def make_model_key(provider_key: str, model_id: str) -> str:
    """A short, name-safe slug for (provider, model): ``me5_multilingual-e5-base``.

    Only the last path segment of a Hugging Face id is kept (the org adds
    length, not identity, next to the provider key). Everything outside
    ``[a-z0-9-]`` becomes ``-`` so the slug is usable inside a Qdrant or
    Chroma collection name.
    """
    prov = re.sub(r"[^a-z0-9]+", "", (provider_key or "").lower()) or "local"
    base = (model_id or "").rstrip("/").rsplit("/", 1)[-1].lower()
    base = re.sub(r"[^a-z0-9-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-") or "model"
    return f"{prov}_{base}"


class LocalEmbeddings:
    # A background batch should hold the model for about this long at most,
    # which bounds how long an interactive query can wait behind it.
    _TARGET_BATCH_S = 0.3
    _MIN_BATCH = 1
    _MAX_BATCH = 64
    # First guess before anything was measured: ~30 chunks/s on a laptop CPU
    # puts 8 passages near the target.
    _INITIAL_BATCH = 8
    # A background batch yields to waiting queries for at most this long, so a
    # stream of queries slows indexing down but can never park it forever.
    _YIELD_MAX_WAIT_S = 5.0

    def __init__(self, provider=None):
        # ``provider`` is injectable for tests; production builds it from the
        # ``[embedding] provider`` config, exactly as before. Imported here, not
        # at module top: app.embeddings → app.utils.logger → app.utils.common
        # imports this module back, so a process whose first import is
        # app.vectorstores (or this module) died on the cycle.
        if provider is None:
            from app.embeddings import create_embedding_provider

            provider = create_embedding_provider()
        self._provider = provider
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._interactive_waiting = 0
        self._batch_size = self._INITIAL_BATCH

    # ── identity ────────────────────────────────────────────────────────────

    @property
    def provider_key(self) -> str:
        key = getattr(self._provider, "PROVIDER_KEY", "") or ""
        return key or type(self._provider).__name__.lower()

    @property
    def model_name(self) -> str:
        return str(getattr(self._provider, "model_name", "") or "")

    @property
    def model_key(self) -> str:
        """Slug naming the model that produced this instance's vectors."""
        return make_model_key(self.provider_key, self.model_name)

    @property
    def dimension(self) -> int:
        return int(self._provider.dimension)

    @property
    def query_prefix(self) -> str:
        return getattr(self._provider, "QUERY_PREFIX", "") or ""

    @property
    def passage_prefix(self) -> str:
        return getattr(self._provider, "PASSAGE_PREFIX", "") or ""

    # ── historical interface (no prefixes; outputs unchanged) ───────────────

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # One lock hold per text, not per list, so a query can slip in
        # between the texts of a long list.
        out: List[List[float]] = []
        for t in texts:
            with self._lock:
                out.append(self._provider.encode(t))
        return out

    def embed_query(self, text: str) -> List[float]:
        # A query is someone waiting on an answer: it jumps ahead of the
        # userdocs background batches.
        with self._interactive():
            return self._provider.encode(text)

    # ── User Document Search ────────────────────────────────────────────────

    def embed_search_query(self, text: str) -> List[float]:
        """Embed a userdocs search query, with the provider's query prompt."""
        with self._interactive():
            return self._provider.encode(self.query_prefix + text)

    def embed_passages(
        self, texts: List[str], *, batch_size: Optional[int] = None,
    ) -> List[List[float]]:
        """Embed passages for a userdocs collection, with the passage prompt.

        Returns one vector per text, in order. ``batch_size=None`` adapts the
        batch so each holds the model for about ``_TARGET_BATCH_S``; a fixed
        size is honoured as given. Before each batch it waits (bounded) while
        an interactive query is queued for the model.
        """
        texts = list(texts)
        if not texts:
            return []
        prefix = self.passage_prefix
        fixed = batch_size is not None and batch_size > 0
        out: List[List[float]] = []
        i = 0
        while i < len(texts):
            self._yield_to_interactive()
            size = int(batch_size) if fixed else self._batch_size
            batch = texts[i:i + size]
            with self._lock:
                t0 = time.monotonic()
                vectors = self._encode_batch(batch, prefix)
                elapsed = time.monotonic() - t0
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"embedding provider returned {len(vectors)} vectors for "
                    f"{len(batch)} texts"
                )
            out.extend(vectors)
            i += len(batch)
            if not fixed:
                self._batch_size = self._next_batch_size(size, len(batch), elapsed)
        return out

    # ── internals ───────────────────────────────────────────────────────────

    def _encode_batch(self, batch: List[str], prefix: str) -> List[List[float]]:
        encode_batch = getattr(self._provider, "encode_batch", None)
        if encode_batch is None:  # a duck-typed provider with encode() only
            return [self._provider.encode(prefix + t) for t in batch]
        return encode_batch(batch, batch_size=len(batch), prefix=prefix)

    def _next_batch_size(self, size: int, n: int, elapsed: float) -> int:
        """The batch size that should take about ``_TARGET_BATCH_S``.

        Shrinks at once (a slow batch is the thing being prevented) but at
        most doubles per step, so one unusually fast batch of short texts
        cannot jump straight to a size that is slow for long ones.
        """
        if n <= 0:
            return size
        per_item = elapsed / n
        ideal = self._MAX_BATCH if per_item <= 0 else int(self._TARGET_BATCH_S / per_item)
        ideal = min(ideal, max(size, 1) * 2)
        return max(self._MIN_BATCH, min(self._MAX_BATCH, ideal))

    @contextmanager
    def _interactive(self) -> Iterator[None]:
        with self._cond:
            self._interactive_waiting += 1
        try:
            with self._lock:
                yield
        finally:
            with self._cond:
                self._interactive_waiting -= 1
                if self._interactive_waiting <= 0:
                    self._interactive_waiting = 0
                    self._cond.notify_all()

    def _yield_to_interactive(self) -> None:
        deadline = time.monotonic() + self._YIELD_MAX_WAIT_S
        with self._cond:
            while self._interactive_waiting > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)
