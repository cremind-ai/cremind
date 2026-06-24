"""Summarization-based conversation-history compaction.

Replaces the old token-window truncation (``limit_messages`` / ``truncate_messages``).
That windowing dropped the oldest turns off the *front* of history every turn, which
broke prompt caching: the cached prefix is a strict prefix match, so mutating the
front invalidates everything after it. Instead this keeps a byte-stable running
*summary* at the front of history and the recent turns verbatim.

State lives on the conversation row (mirrors the memory watermark):

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

This runs **synchronously** in the consumption path (not as a background task like
memory extraction) so the threshold is never exceeded — important because, on the
first turn after this feature ships, a long pre-existing conversation would otherwise
be sent whole. Per-conversation turns are serialized by ``app.events.queue``, so the
inline read-modify-write never races on a conversation's watermark.

The summary is injected as a **user**-role message (not ``system``): the Anthropic
provider folds every ``system`` message into the cached system block (see
``app.lib.llm.anthropic._convert_messages``), which would couple the summary with the
instruction; a user message stays a distinct, byte-stable history block.
"""

from __future__ import annotations

from typing import Any, cast

from app.config.user_config import CompactionConfig, resolve_compaction_config
from app.constants import ChatCompletionTypeEnum
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


async def _summarize(
    cremind_agent: Any, old_summary: str | None, folded: list[dict],
    profile: str, cfg: CompactionConfig,
) -> str:
    """One-shot summary via the low model group (mirrors summary.summarize_reasoning)."""
    llm = cremind_agent.low_group_llm(profile)
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": _render_fold_input(old_summary, folded)},
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
                new_summary = await _summarize(cremind_agent, summary, folded, profile, cfg)
                if new_summary:
                    new_summary = truncate_to_tokens(new_summary, cfg.max_tokens)
                    new_watermark = int(folded[-1]["ordering"])
                    await conversation_storage.set_compaction_state(
                        conversation_id, new_summary, new_watermark,
                    )
                    summary, tail = new_summary, kept
                    logger.info(
                        f"[compaction] conv={conversation_id} folded {len(folded)} msg(s), "
                        f"watermark->{new_watermark}, eff_tokens={eff_tokens}"
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
