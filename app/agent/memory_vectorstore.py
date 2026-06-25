"""Long-term memory in the vector store (embedding-enabled mode).

When ``embedding.enabled`` is on, long-term memory lives in the vector store
instead of the size-capped DB queue. Storage is effectively unlimited, so the
extraction prompt is FLEXIBLE (see :mod:`app.agent.memory_extractor`) and
retrieval is a top-K similarity query against the latest user message rather
than a FIFO read.

Collection ``long_term_memory_{profile}`` (per-profile, mirroring
``tool_embeddings_{profile}``). It is deliberately NOT registered under
``embedding_lifecycle._OWNED_COLLECTION_PREFIXES`` — a model re-sync rebuilds
tool embeddings from their source, but memory has no source to rebuild from, so
it must survive re-syncs.

These ops are synchronous (like the tool-embeddings path). Every public function
no-ops gracefully when embedding is unavailable, so a vector failure never breaks
the load-bearing compaction-summary write.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.config.embedding_state import embedding_state
from app.config.settings import BaseConfig
from app.utils.logger import logger

# Skip storing a fact whose nearest neighbour is at least this similar — vector
# dedup that tolerates the flexible prompt's paraphrasing (exact-string dedup
# would not). Cosine similarity; tune after dogfooding.
_DEDUP_THRESHOLD = 0.95


def _collection_name(profile: str) -> str:
    return f"long_term_memory_{profile}"


def _fact_id(profile: str, fact: str) -> str:
    """Content-stable id so re-adding the same fact upserts instead of
    duplicating, even if similarity dedup misses it."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cremind-ltm://{profile}/{fact}"))


def vector_long_term_available(agent: Any) -> bool:
    """True when long-term memory should use the vector store this turn.

    Requires embedding enabled server-wide, the subsystem READY (not mid-load/
    rebuild), and the agent wired with live embedding + vector-store handles.
    """
    if agent is None:
        return False
    if not BaseConfig.is_embedding_enabled() or not embedding_state.is_ready():
        return False
    return getattr(agent, "embedding", None) is not None and getattr(agent, "vector_store", None) is not None


def store_long_term(
    *,
    agent: Any,
    profile: str,
    conversation_id: str | None,
    facts: list[str],
) -> int:
    """Store new long-term facts in the vector store; return the count added.

    Skips facts whose nearest stored neighbour is within ``_DEDUP_THRESHOLD``.
    No FIFO eviction — vector storage is effectively unlimited. Any failure is
    logged and swallowed (returns 0) so the caller's summary write still lands.
    """
    facts = [f.strip() for f in facts if f and f.strip()]
    if not facts or not vector_long_term_available(agent):
        return 0
    vs = agent.vector_store
    emb = agent.embedding
    coll = _collection_name(profile)
    try:
        vectors = emb.embed_documents(facts)
        if not vectors:
            return 0
        exists = vs.collection_exists(coll)
        if not exists:
            vs.create_named_collection(coll, size=len(vectors[0]))
        created_at = time.time()
        points: list[dict[str, Any]] = []
        for fact, vec in zip(facts, vectors):
            if exists:
                hits = vs.query_by_vector(
                    collection_name=coll, vector=vec, limit=1, filter={"profile": profile},
                )
                if hits and float(hits[0].get("score") or 0.0) >= _DEDUP_THRESHOLD:
                    continue
            pid = _fact_id(profile, fact)
            points.append({
                "id": pid,
                "vector": list(vec),
                "payload": {
                    "key": pid,
                    "text": fact,
                    "profile": profile,
                    "source_conversation_id": conversation_id,
                    "created_at": created_at,
                },
            })
        if points:
            vs.add_points(collection_name=coll, points=points)
        return len(points)
    except Exception:  # noqa: BLE001
        logger.exception(f"[memory] vector store_long_term failed for profile={profile}")
        return 0


def retrieve_long_term(
    *,
    agent: Any,
    profile: str,
    query_text: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Top-``limit`` long-term facts most similar to ``query_text``.

    Returns ``[{content, created_at}, ...]`` sorted oldest-first (deterministic),
    matching the shape :func:`app.agent.memory_runner.build_memory_block` expects.
    Empty on any failure, empty query, or missing collection.
    """
    query_text = (query_text or "").strip()
    if not query_text or not vector_long_term_available(agent):
        return []
    vs = agent.vector_store
    emb = agent.embedding
    coll = _collection_name(profile)
    try:
        if not vs.collection_exists(coll):
            return []
        vec = emb.embed_query(query_text)
        hits = vs.query_by_vector(
            collection_name=coll, vector=vec, limit=limit, filter={"profile": profile},
        )
        out = [
            {"content": (h.get("text") or "").strip(), "created_at": h.get("created_at")}
            for h in hits
            if (h.get("text") or "").strip()
        ]
        out.sort(key=lambda e: e.get("created_at") or 0)
        return out
    except Exception:  # noqa: BLE001
        logger.exception(f"[memory] vector retrieve_long_term failed for profile={profile}")
        return []


def list_long_term(*, agent: Any, profile: str, limit: int = 50) -> list[dict[str, Any]]:
    """Enumerate stored long-term facts (newest ``limit``) for the memory panel.

    Unlike :func:`retrieve_long_term` this is not similarity-ranked — it lists the
    most recent facts so the UI can show what is stored. ``[{content, created_at}]``
    sorted oldest-first. Empty on any failure or missing collection.
    """
    if not vector_long_term_available(agent):
        return []
    vs = agent.vector_store
    coll = _collection_name(profile)
    try:
        if not vs.collection_exists(coll):
            return []
        points = vs.list_all_points(coll, with_vectors=False, filter={"profile": profile})
        rows = [
            {"content": (p["payload"].get("text") or "").strip(),
             "created_at": p["payload"].get("created_at")}
            for p in points
            if (p.get("payload") or {}).get("text")
        ]
        rows.sort(key=lambda e: e.get("created_at") or 0)
        return rows[-limit:] if limit and len(rows) > limit else rows
    except Exception:  # noqa: BLE001
        logger.exception(f"[memory] vector list_long_term failed for profile={profile}")
        return []
