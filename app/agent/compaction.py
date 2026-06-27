"""Conversation compaction — model-driven via the ``compact_conversation`` tool.

History compaction keeps a byte-stable running *summary* at the front of history
plus the recent turns verbatim, so the cached prompt prefix is reused across
turns (a strict prefix match: mutating the front invalidates everything after).

State lives on the conversation row:

- ``compaction_summary``   — running summary of every message ``ordering <= watermark``
- ``compaction_watermark`` — ordering of the newest message folded into the summary
  (``-1`` means nothing folded yet)

Compaction is now **suggest-only and model-driven**:

- :func:`build_compacted_history` (read path) rebuilds ``[summary] + verbatim
  tail`` each turn from the stored state. It never folds automatically.
- :func:`compaction_suggestion` reports when ``summary + tail`` crosses the
  threshold so the UI can propose compacting (the user clicks; it is never
  forced).
- When the user (or a synthetic "please compact" turn) triggers it, the **main
  model** — with the whole conversation already in its cached prefix — writes the
  new running summary and any durable long-term facts as the arguments of the
  ``compact_conversation`` tool, whose ``run()`` calls :func:`apply_compaction`.
  Reusing the cached prefix is what makes compaction cheap.

The summary is injected as a **user**-role message (not ``system``): the Anthropic
provider folds every ``system`` message into the cached system block, which would
couple the summary with the instruction; a user message stays a distinct,
byte-stable history block.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config.user_config import (
    replay_reasoning_enabled,
    resolve_compaction_config,
    resolve_memory_config,
)
from app.lib.llm.pricing import DEFAULT_CONTEXT_WINDOW, context_window_for
from app.storage import get_memory_storage
from app.utils.common import (
    convert_db_messages_to_history,
    count_content_tokens,
    truncate_to_tokens,
)
from app.utils.logger import logger

# Frames the running summary for the model. Kept stable so the cached prefix only
# changes when the summary text itself changes (i.e. on a compaction event).
_SUMMARY_HEADER = (
    "[Summary of earlier conversation, compacted to save context. Treat it as "
    "factual background of what was already said and done.]\n\n"
)


def _build_effective(
    summary: str | None, tail: list[dict], *, include_reasoning: bool = False,
) -> list[Any]:
    """Effective history = optional summary message + verbatim tail.

    The tail goes through ``convert_db_messages_to_history`` (replaying each turn's
    native reasoning trace when ``include_reasoning`` is set); the summary is
    prepended as a synthetic user message.
    """
    history = convert_db_messages_to_history(tail, include_reasoning=include_reasoning)
    if summary:
        history = [{"role": "user", "content": _SUMMARY_HEADER + summary}] + history
    return history


def context_tokens_from_records(records: list[dict] | None) -> int | None:
    """Model-reported prompt size of a turn's FINAL reasoning call.

    A turn fans out to several LLM calls; the last one (highest ``step_index``) has
    the largest prompt and is what the model just processed. Return its full prompt
    size = ``input + cache_read + cache_creation`` (the per-turn ``token_usage`` blob
    is summed across calls and over-counts, so it can't be used). ``None`` when there
    are no reasoning records.
    """
    if not records:
        return None
    reasoning = [r for r in records if (r.get("source_kind") or "reasoning") == "reasoning"]
    if not reasoning:
        return None
    last = max(reasoning, key=lambda r: int(r.get("step_index") or 0))
    return (
        int(last.get("input_tokens") or 0)
        + int(last.get("cache_read_input_tokens") or 0)
        + int(last.get("cache_creation_input_tokens") or 0)
    )


async def build_compacted_history(
    *,
    conversation_id: str,
    profile: str,
    conversation_storage: Any,
    cremind_agent: Any = None,
    fallback_history: list[Any],
    exclude_message_id: str | None = None,
) -> list[Any]:
    """Return the effective history (running summary + verbatim tail).

    Applies the stored compaction state only — it does NOT fold automatically
    (that happens model-driven via the ``compact_conversation`` tool).

    ``exclude_message_id`` is the current turn's just-persisted message: the agent
    receives that turn as its volatile input, so it must be dropped from the tail
    or it would appear twice. Falls back to ``fallback_history`` on disable/error.
    """
    try:
        cfg = resolve_compaction_config(profile)
    except Exception:  # noqa: BLE001
        logger.exception(f"[compaction] could not resolve config for profile={profile}")
        return fallback_history
    if not cfg.enabled:
        return fallback_history

    try:
        include_reasoning = replay_reasoning_enabled(profile)
        summary, watermark, _ = await conversation_storage.get_compaction_state(conversation_id)
        tail = await conversation_storage.get_messages_after(conversation_id, watermark)
        if exclude_message_id:
            tail = [m for m in tail if m.get("id") != exclude_message_id]
        return _build_effective(summary, tail, include_reasoning=include_reasoning)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[compaction] failed for conv={conversation_id}; falling back to raw history"
        )
        return fallback_history


async def context_usage(
    *,
    conversation_id: str,
    profile: str,
    conversation_storage: Any,
) -> dict:
    """Return ``{current_tokens, context_window, threshold}`` for the conversation.

    - ``current_tokens``: the model's actually-reported prompt size for the latest
      turn (``context_tokens`` persisted on the newest agent message — the final
      reasoning call's ``input + cache_read + cache_creation``). This is exactly the
      context the model processed, so the ratio against the window is accurate. ``0``
      when no turn has run yet (or a message predates this metric — self-heals next turn).
    - ``context_window``: the active model's context window, looked up from the
      provider catalog using the latest agent message's stamped ``provider``/``model``;
      falls back to :data:`DEFAULT_CONTEXT_WINDOW`.
    - ``threshold``: ``round(compact_threshold_percent / 100 * context_window)`` —
      compaction is suggested once ``current_tokens`` reaches it. The percentage
      (default 85) keeps the prompt clear of the window and leaves headroom for the
      response.

    Single source of truth shared by the chat banner (:func:`compaction_suggestion`)
    and the ``/memory`` endpoint. Defensive — never raises; falls back to safe defaults.
    """
    try:
        cfg = resolve_compaction_config(profile)
        percent = cfg.compact_threshold_percent
    except Exception:  # noqa: BLE001
        percent = 85.0

    current = 0
    provider = model = None
    try:
        latest = await conversation_storage.get_latest_agent_message(conversation_id)
        if latest:
            tu = latest.get("token_usage") or {}
            current = int(tu.get("context_tokens") or 0)
            meta = latest.get("metadata") or {}
            provider, model = meta.get("provider"), meta.get("model")
    except Exception:  # noqa: BLE001
        logger.debug(f"[compaction] could not read latest message for conv={conversation_id}",
                     exc_info=True)

    window = context_window_for(provider, model) or DEFAULT_CONTEXT_WINDOW
    threshold = max(1, round(percent / 100 * window))
    return {"current_tokens": current, "context_window": window, "threshold": threshold}


async def compaction_suggestion(
    *,
    conversation_id: str,
    profile: str,
    conversation_storage: Any,
) -> dict | None:
    """Return a suggestion payload when the model's context crosses the threshold.

    ``{current_tokens, threshold, estimated_savings, context_window}`` when compaction
    is worth proposing, else ``None``. Never folds — purely advisory for the UI popup.
    """
    try:
        cfg = resolve_compaction_config(profile)
    except Exception:  # noqa: BLE001
        return None
    if not cfg.enabled:
        return None
    try:
        usage = await context_usage(
            conversation_id=conversation_id,
            profile=profile,
            conversation_storage=conversation_storage,
        )
        current, threshold = usage["current_tokens"], usage["threshold"]
        if current < threshold:
            return None
        # Roughly the tokens that compaction would fold into the summary.
        savings = max(0, current - cfg.keep_recent_tokens - cfg.max_tokens)
        return {
            "current_tokens": current,
            "threshold": threshold,
            "estimated_savings": savings,
            "context_window": usage["context_window"],
        }
    except Exception:  # noqa: BLE001
        logger.exception(f"[compaction] suggestion check failed for conv={conversation_id}")
        return None


async def apply_compaction(
    *,
    conversation_id: str,
    profile: str,
    summary: str,
    long_term: list[str] | None,
    conversation_storage: Any,
) -> dict:
    """Persist a model-generated running summary + any long-term facts.

    Called by the ``compact_conversation`` tool's ``run()``. The watermark
    advances to the newest persisted message (the summary now covers everything
    up to now); the verbatim tail collapses on the next turn.
    """
    cfg = resolve_compaction_config(profile)
    summary_old, watermark_old, _ = await conversation_storage.get_compaction_state(conversation_id)

    all_msgs = await conversation_storage.get_messages_after(conversation_id, -1)
    watermark = max(
        (int(m["ordering"]) for m in all_msgs if m.get("ordering") is not None),
        default=watermark_old,
    )

    summary = (summary or "").strip()
    new_summary = truncate_to_tokens(summary, cfg.max_tokens) if summary else summary_old
    await conversation_storage.set_compaction_state(conversation_id, new_summary, watermark)

    stored = await _store_long_term_facts(profile, conversation_id, long_term or [])
    logger.info(
        f"[compaction] applied for conv={conversation_id}: watermark->{watermark}, "
        f"summary_chars={len(new_summary or '')}, long_term+={stored}"
    )
    return {
        "watermark": watermark,
        "summary_chars": len(new_summary or ""),
        "long_term_stored": stored,
    }


async def _store_long_term_facts(
    profile: str, conversation_id: str, facts: list[str],
) -> int:
    """Persist long-term facts to the vector store (embedding on) or DB queue (off)."""
    facts = [f.strip() for f in facts if f and f.strip()]
    if not facts:
        return 0

    from types import SimpleNamespace
    from app.config.embedding_state import embedding_state
    from app.agent import memory_vectorstore

    shim = SimpleNamespace(
        embedding=embedding_state.embedding, vector_store=embedding_state.vector_store,
    )
    if memory_vectorstore.vector_long_term_available(shim):
        return await asyncio.to_thread(
            memory_vectorstore.store_long_term,
            agent=shim, profile=profile, conversation_id=conversation_id, facts=facts,
        )

    try:
        memory_cfg = resolve_memory_config(profile)
    except Exception:  # noqa: BLE001
        memory_cfg = None
    max_t = memory_cfg.long_term_max_tokens if memory_cfg else 256
    qsize = memory_cfg.long_term_queue_size if memory_cfg else 100
    storage = get_memory_storage()
    n = 0
    for fact in facts:
        clipped = truncate_to_tokens(fact, max_t).strip()
        if not clipped:
            continue
        await storage.add_long_term(
            profile=profile, content=clipped,
            token_count=count_content_tokens(clipped),
            source_conversation_id=conversation_id, queue_size=qsize,
        )
        n += 1
    return n
