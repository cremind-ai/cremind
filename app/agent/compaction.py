"""Unified conversation compaction + memory generation.

Replaces the old token-window truncation (``limit_messages`` / ``truncate_messages``).
That windowing dropped the oldest turns off the *front* of history every turn, which
broke prompt caching: the cached prefix is a strict prefix match, so mutating the
front invalidates everything after it. Instead this keeps a byte-stable running
*summary* at the front of history and the recent turns verbatim.

The running summary IS the conversation's "short-term memory" — the two were
unified (they both summarized the conversation per-conversation). One LLM pass at
the fold point produces it:

- **memory disabled** → a plain chat completion (:func:`_summarize_plain`) yields
  the running summary only. This is the conversation compact.
- **memory enabled** → a forced ``save_memory`` tool call
  (:func:`app.agent.memory_extractor.extract_fold_memory`) yields the running
  summary AND long-term facts in one call. Long-term is routed to the vector store
  (embedding on, FLEXIBLE prompt) or the DB queue (embedding off, STRICT prompt).

State lives on the conversation row:

- ``compaction_summary``   — running summary of every message ``ordering <= watermark``
- ``compaction_watermark`` — ordering of the newest message folded into the summary
  (``-1`` means nothing folded yet)

Each turn, :func:`build_compacted_history` reads that state, rebuilds the verbatim
tail (``ordering > watermark``) and — when the tail's tokens cross
``compact_threshold_tokens`` — folds the oldest tail messages into the summary (one
LLM call), advances the watermark, and persists, all *before* the turn's prompt is
assembled. The fold target ``keep_recent_tokens`` sits well below the trigger so the
summary stays byte-identical for many turns and the cached prefix is reused; a
compaction event is a deliberate one-time cache write.

This runs **synchronously** in the consumption path so the threshold is never
exceeded — important because, on the first turn after this feature ships, a long
pre-existing conversation would otherwise be sent whole. Per-conversation turns are
serialized by ``app.events.queue``, so the inline read-modify-write never races on a
conversation's watermark. The manual-trigger path (:func:`force_fold`) runs off that
queue, so it takes a per-conversation lock.

The summary is injected as a **user**-role message (not ``system``): the Anthropic
provider folds every ``system`` message into the cached system block (see
``app.lib.llm.anthropic._convert_messages``), which would couple the summary with the
instruction; a user message stays a distinct, byte-stable history block.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

from app.config.settings import BaseConfig
from app.config.user_config import (
    CompactionConfig,
    MemoryConfig,
    resolve_compaction_config,
    resolve_memory_config,
)
from app.constants import ChatCompletionTypeEnum
from app.storage import get_memory_storage
from app.utils.common import (
    convert_db_messages_to_history,
    count_content_tokens,
    truncate_to_tokens,
)
from app.utils.logger import logger

# Headroom (tokens) added to the LLM-call budget on the memory-enabled path so the
# forced tool call can emit the long-term facts alongside the running summary
# (which alone may approach ``compaction.max_tokens``).
_LONG_TERM_OUTPUT_BUDGET = 512

# Frames the running summary for the model. Kept stable so the cached prefix only
# changes when the summary text itself changes (i.e. on a compaction event).
_SUMMARY_HEADER = (
    "[Summary of earlier conversation, compacted to save context. Treat it as "
    "factual background of what was already said and done.]\n\n"
)

# Plain-completion system prompt (memory-disabled path). The memory-enabled path
# uses the merged save_memory prompt in app.agent.memory_extractor instead.
_SUMMARY_SYSTEM = (
    "You maintain a running summary of a long conversation between a user and an AI "
    "agent so the agent keeps full continuity after older turns are dropped from its "
    "context window.\n\n"
    "You are given the PRIOR SUMMARY (already covers the earliest part of the "
    "conversation) and a batch of OLDER MESSAGES that now need to be folded in. "
    "Produce ONE merged summary that supersedes the prior summary and absorbs the new "
    "messages.\n\n"
    "Preserve, densely and specifically: facts and decisions; identifiers (IDs, file "
    "paths, URLs, ticket/PR numbers, command names, config keys, exact values); "
    "unresolved questions and pending TODOs; the user's stated goals, constraints, and "
    "preferences; and any state the agent must not re-derive. Carry forward everything "
    "still relevant from the prior summary — do not drop it just because it is old. Do "
    "not invent anything not present in the inputs. Output GitHub-flavored Markdown, "
    "with no preamble."
)

# Per-conversation locks for the off-queue manual-trigger path (force_fold).
_force_locks: dict[str, asyncio.Lock] = {}


@dataclass
class _FoldResult:
    summary: str
    long_term: list[str] = field(default_factory=list)


def _render_fold_input(old_summary: str | None, folded: list[dict]) -> str:
    """Render the prior summary + the messages being folded, as the summarizer input."""
    parts: list[str] = []
    if old_summary:
        parts.append("## PRIOR SUMMARY\n" + old_summary)
    lines: list[str] = []
    for m in folded:
        role = "assistant" if m.get("role") == "agent" else (m.get("role") or "user")
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"### {role}\n{content}")
    parts.append("## OLDER MESSAGES TO FOLD IN\n" + "\n\n".join(lines))
    return "\n\n".join(parts)


async def _summarize_plain(llm: Any, fold_input: str, cfg: CompactionConfig) -> str:
    """One-shot running summary via the low model group (memory-disabled path)."""
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": fold_input},
    ]
    collected: list[str] = []
    async for resp in llm.chat_completion(
        messages=cast(Any, messages),
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        retry=cfg.retry,
    ):
        if resp.get("type") == ChatCompletionTypeEnum.CONTENT:
            data = resp.get("data")
            if data:
                collected.append(data)
    return "".join(collected).strip()


async def _fold(
    cremind_agent: Any,
    old_summary: str | None,
    folded: list[dict],
    profile: str,
    comp_cfg: CompactionConfig,
    memory_cfg: MemoryConfig | None,
) -> _FoldResult | None:
    """Produce the merged running summary (+ long-term facts when memory is on).

    Returns ``None`` when the LLM produced nothing usable — the caller leaves the
    summary + watermark unchanged and sends the full tail this turn.
    """
    llm = cremind_agent.low_group_llm(profile)
    fold_input = _render_fold_input(old_summary, folded)

    if memory_cfg is None or not memory_cfg.enabled:
        summary = await _summarize_plain(llm, fold_input, comp_cfg)
        return _FoldResult(summary=summary) if summary else None

    # Memory enabled: forced save_memory tool call → summary + long-term in one call.
    flexible = BaseConfig.is_embedding_enabled()
    if flexible:
        # Vector store dedups at write time; no need to feed the full set back.
        current_long_term: list[str] = []
    else:
        storage = get_memory_storage()
        current_long_term = [m["content"] for m in await storage.get_long_term(profile)]

    from app.agent.memory_extractor import extract_fold_memory

    parsed = await extract_fold_memory(
        llm=llm,
        fold_input=fold_input,
        long_term_flexible=flexible,
        current_long_term=current_long_term,
        summary_max_tokens=comp_cfg.max_tokens,
        long_max_tokens=memory_cfg.long_term_max_tokens,
        temperature=comp_cfg.temperature,
        max_tokens=comp_cfg.max_tokens + _LONG_TERM_OUTPUT_BUDGET,
        retry=comp_cfg.retry,
    )
    if parsed is None or not parsed.get("short_term_memory"):
        return None
    return _FoldResult(
        summary=parsed["short_term_memory"],
        long_term=parsed.get("long_term_memories") or [],
    )


async def _store_long_term(
    cremind_agent: Any,
    profile: str,
    conversation_id: str,
    facts: list[str],
    memory_cfg: MemoryConfig,
) -> None:
    """Route extracted long-term facts to the vector store or the DB queue."""
    if not facts:
        return
    from app.agent import memory_vectorstore

    if memory_vectorstore.vector_long_term_available(cremind_agent):
        await asyncio.to_thread(
            memory_vectorstore.store_long_term,
            agent=cremind_agent,
            profile=profile,
            conversation_id=conversation_id,
            facts=facts,
        )
        return

    storage = get_memory_storage()
    for fact in facts:
        clipped = truncate_to_tokens(fact, memory_cfg.long_term_max_tokens).strip()
        if not clipped:
            continue
        await storage.add_long_term(
            profile=profile,
            content=clipped,
            token_count=count_content_tokens(clipped),
            source_conversation_id=conversation_id,
            queue_size=memory_cfg.long_term_queue_size,
        )


def _select_fold_count(tail: list[dict], cfg: CompactionConfig) -> int:
    """How many of the oldest tail messages to fold so the remaining tail drops to
    ~``keep_recent_tokens``, never folding below ``keep_recent_messages`` recents."""
    n = len(tail)
    toks = [count_content_tokens(m.get("content") or "") for m in tail]
    remaining = sum(toks)
    cut = 0
    while cut < n - cfg.keep_recent_messages and remaining > cfg.keep_recent_tokens:
        remaining -= toks[cut]
        cut += 1
    return cut


def _build_effective(summary: str | None, tail: list[dict]) -> list[Any]:
    """Effective history = optional summary message + verbatim tail.

    The tail goes through ``convert_db_messages_to_history`` (so recent messages keep
    their ``message_id`` for the message_detail tool); the summary is prepended as a
    synthetic user message with no id.
    """
    history = convert_db_messages_to_history(tail, inject_ids=True)
    if summary:
        history = [{"role": "user", "content": _SUMMARY_HEADER + summary}] + history
    return history


async def build_compacted_history(
    *,
    conversation_id: str,
    profile: str,
    conversation_storage: Any,
    cremind_agent: Any,
    fallback_history: list[Any],
    exclude_message_id: str | None = None,
) -> list[Any]:
    """Return the effective history (running summary + verbatim tail), compacting
    first when the tail is over threshold.

    ``exclude_message_id`` is the current turn's just-persisted message: the agent
    receives that turn as its volatile input, so it must be dropped from the history
    tail or it would appear twice (and be counted toward the threshold / folded).

    Returns ``fallback_history`` unchanged when compaction is disabled or on any
    error (so a failure degrades to the raw passed-in history rather than breaking
    the turn).
    """
    try:
        cfg = resolve_compaction_config(profile)
    except Exception:  # noqa: BLE001
        logger.exception(f"[compaction] could not resolve config for profile={profile}")
        return fallback_history
    if not cfg.enabled:
        return fallback_history

    try:
        memory_cfg = resolve_memory_config(profile)
    except Exception:  # noqa: BLE001
        memory_cfg = None

    try:
        summary, watermark, _ = await conversation_storage.get_compaction_state(conversation_id)
        tail = await conversation_storage.get_messages_after(conversation_id, watermark)
        if exclude_message_id:
            # Drop the current turn (sent separately as the volatile input).
            tail = [m for m in tail if m.get("id") != exclude_message_id]

        eff_tokens = count_content_tokens(summary or "") + sum(
            count_content_tokens(m.get("content") or "") for m in tail
        )
        if eff_tokens >= cfg.compact_threshold_tokens:
            cut = _select_fold_count(tail, cfg)
            if cut > 0:
                folded, kept = tail[:cut], tail[cut:]
                result = await _fold(cremind_agent, summary, folded, profile, cfg, memory_cfg)
                if result and result.summary:
                    new_summary = truncate_to_tokens(result.summary, cfg.max_tokens)
                    new_watermark = int(folded[-1]["ordering"])
                    await conversation_storage.set_compaction_state(
                        conversation_id, new_summary, new_watermark,
                    )
                    summary, tail = new_summary, kept
                    if memory_cfg is not None and memory_cfg.enabled and result.long_term:
                        await _store_long_term(
                            cremind_agent, profile, conversation_id, result.long_term, memory_cfg,
                        )
                    logger.info(
                        f"[compaction] conv={conversation_id} folded {len(folded)} msg(s), "
                        f"watermark->{new_watermark}, eff_tokens={eff_tokens}, "
                        f"long_term+={len(result.long_term)}"
                    )
                else:
                    # Summarization failed/empty — leave summary + watermark
                    # unchanged (no data lost) and send the full tail this turn.
                    logger.warning(
                        f"[compaction] empty summary for conv={conversation_id}; "
                        f"sending full tail this turn"
                    )

        return _build_effective(summary, tail)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[compaction] failed for conv={conversation_id}; falling back to raw history"
        )
        return fallback_history


async def force_fold(
    *,
    conversation_id: str,
    profile: str,
    conversation_storage: Any,
    cremind_agent: Any,
) -> bool:
    """Manually fold the current tail regardless of threshold (manual trigger).

    Folds everything except the most recent ``keep_recent_messages`` so a short
    conversation that never crosses ``compact_threshold_tokens`` can still have its
    memory extracted on demand. Returns ``True`` when a fold happened.

    Runs off the per-conversation run queue, so it takes a per-conversation lock to
    avoid racing the inline path on the watermark.
    """
    cfg = resolve_compaction_config(profile)
    if not cfg.enabled:
        return False
    try:
        memory_cfg = resolve_memory_config(profile)
    except Exception:  # noqa: BLE001
        memory_cfg = None

    lock = _force_locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        try:
            summary, watermark, _ = await conversation_storage.get_compaction_state(conversation_id)
            tail = await conversation_storage.get_messages_after(conversation_id, watermark)
            cut = max(0, len(tail) - cfg.keep_recent_messages)
            if cut <= 0:
                return False
            folded = tail[:cut]
            result = await _fold(cremind_agent, summary, folded, profile, cfg, memory_cfg)
            if not (result and result.summary):
                return False
            new_summary = truncate_to_tokens(result.summary, cfg.max_tokens)
            new_watermark = int(folded[-1]["ordering"])
            await conversation_storage.set_compaction_state(
                conversation_id, new_summary, new_watermark,
            )
            if memory_cfg is not None and memory_cfg.enabled and result.long_term:
                await _store_long_term(
                    cremind_agent, profile, conversation_id, result.long_term, memory_cfg,
                )
            logger.info(
                f"[compaction] force_fold conv={conversation_id} folded {len(folded)} msg(s), "
                f"watermark->{new_watermark}, long_term+={len(result.long_term)}"
            )
            return True
        except Exception:  # noqa: BLE001
            logger.exception(f"[compaction] force_fold failed for conv={conversation_id}")
            return False
