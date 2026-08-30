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
import json
import time
from typing import Any

from app.config.user_config import (
    replay_reasoning_enabled,
    resolve_compaction_config,
    resolve_memory_config,
)
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.pricing import (
    DEFAULT_CONTEXT_WINDOW,
    context_window_for,
    max_output_tokens_for,
)
from app.storage import get_memory_storage
from app.utils.common import (
    convert_db_messages_to_history,
    count_content_tokens,
    truncate_to_tokens,
    truncate_to_tokens_tail,
)
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var

# Rough fixed reserves (tokens). These are approximations; the deterministic
# floor + the runtime catch-and-retry are the real guarantee, so a modest
# over/under here only shifts when a fold is *suggested*, never correctness.
_PER_MESSAGE_OVERHEAD = 4          # role/framing tokens per wire message
_SYSTEM_RESERVE_TOKENS = 3000      # system prompt + tool-schema JSON not in `history`
DEFAULT_RESPONSE_RESERVE = 8000    # room kept for the reply when a model omits max_output_tokens

# Serialize a conversation's fold read-modify-write against a concurrent fold (e.g.
# a manual "compact now" overlapping an auto-fold). Sufficient for the single-process
# / single-uvicorn-worker deployment; multi-worker Postgres additionally relies on the
# per-conversation turn worker for ordering (see run_model_fold / stream_runner).
_compaction_locks: dict[str, "asyncio.Lock"] = {}


def _compaction_lock(conversation_id: str) -> "asyncio.Lock":
    lock = _compaction_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _compaction_locks[conversation_id] = lock
    return lock

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


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def _wire_message_tokens(msg: Any) -> int:
    """Approximate wire tokens for one assembled (OpenAI-shaped) message.

    Counts the bytes that actually go on the wire — ``tool_calls`` argument JSON
    and ``tool`` result bodies — not just ``content``. This is the crux fix: with
    reasoning replay on (the default), a turn's row is expanded into its full
    native ``llm_messages`` trace, so a ``content``-only ruler undercounts it 2-10x.
    """
    if not isinstance(msg, dict):
        return count_content_tokens(str(msg)) + _PER_MESSAGE_OVERHEAD

    total = 0
    content = msg.get("content")
    if isinstance(content, str):
        total += count_content_tokens(content)
    elif isinstance(content, list):
        # Defensive: Anthropic-style content blocks (text / tool_use / tool_result).
        for block in content:
            if isinstance(block, str):
                total += count_content_tokens(block)
            elif isinstance(block, dict):
                total += count_content_tokens(block.get("text") or "")
                inner = block.get("content")
                if isinstance(inner, str):
                    total += count_content_tokens(inner)
                elif isinstance(inner, list):
                    for ib in inner:
                        if isinstance(ib, dict):
                            total += count_content_tokens(ib.get("text") or "")
                        elif isinstance(ib, str):
                            total += count_content_tokens(ib)
                if block.get("input") is not None:
                    total += count_content_tokens(_safe_json(block.get("input")))

    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        total += count_content_tokens(fn.get("name") or "")
        args = fn.get("arguments")
        total += count_content_tokens(
            args if isinstance(args, str) else _safe_json(args)
        )

    return total + _PER_MESSAGE_OVERHEAD


def estimate_prompt_tokens(history: list | None, *, system_reserve: int = 0) -> int:
    """Trace-aware size of an assembled request — the single shared ruler.

    Used by the suggestion banner, the pre-flight check, the deterministic floor
    and the fold-target clamp so they can never disagree about how big the prompt
    is. Sizes messages exactly as assembled for the wire (native ``tool_calls``
    argument JSON + ``tool`` result bodies included), unlike
    :func:`app.utils.common.count_content_tokens`, which sees only a row's
    ``content`` string. ``system_reserve`` accounts for the system block +
    tool-schema JSON the provider prepends but that are not part of ``history``.
    """
    return sum(_wire_message_tokens(m) for m in (history or [])) + max(0, system_reserve)


def response_reserve_for(provider: str | None, model: str | None) -> int:
    """Tokens to hold back for the model's reply so the prompt can't crowd it out.

    From the catalog ``max_output_tokens`` when present, else
    :data:`DEFAULT_RESPONSE_RESERVE`.
    """
    return max_output_tokens_for(provider, model) or DEFAULT_RESPONSE_RESERVE


# ── Deterministic emergency floor (L3) ──
#
# A model-independent, boundary-safe clipper that guarantees an assembled request
# fits under a token ceiling. It is deterministic in its input (same history +
# ceiling → byte-identical output), so a sustained overflow yields a stable prefix
# the prompt-cache still rewards rather than a per-turn cache thrash.

_TRUNC_NOTICE = "\n[... truncated to fit the context window ...]\n"
_OMIT_NOTE = "[Earlier turns omitted to fit the model's context window.]"
_TOOL_PAYLOAD_CAP_TOKENS = 256   # per tool_result / tool_call-arg cap when truncating


def _is_summary_message(msg: Any) -> bool:
    return (
        isinstance(msg, dict)
        and msg.get("role") == "user"
        and isinstance(msg.get("content"), str)
        and msg["content"].startswith(_SUMMARY_HEADER)
    )


def _split_turns(messages: list) -> list[list]:
    """Group wire messages into turns, each starting at a ``user`` message.

    A turn is ``[user, assistant(+tool_calls), tool*, assistant, ...]`` up to the
    next user message, so dropping a whole turn never orphans a ``tool`` result
    from its ``tool_calls``. Leading non-user messages (defensive) form turn 0.
    """
    turns: list[list] = []
    cur: list = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user" and cur:
            turns.append(cur)
            cur = [m]
        else:
            cur.append(m)
    if cur:
        turns.append(cur)
    return turns


def _clip_text(text: str, max_tokens: int) -> str:
    if not isinstance(text, str) or max_tokens <= 0:
        return text
    if count_content_tokens(text) <= max_tokens:
        return text
    return truncate_to_tokens(text, max_tokens) + _TRUNC_NOTICE


def _truncate_tool_payloads(body: list, *, args: bool) -> list:
    """Head-clip oversized ``tool`` result bodies (and, when ``args``, ``tool_calls``
    argument JSON) to :data:`_TOOL_PAYLOAD_CAP_TOKENS` each. Pure — returns a copy."""
    out: list = []
    for m in body:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m2 = dict(m)
        if m2.get("role") == "tool" and isinstance(m2.get("content"), str):
            m2["content"] = _clip_text(m2["content"], _TOOL_PAYLOAD_CAP_TOKENS)
        if args and m2.get("tool_calls"):
            new_calls = []
            for tc in m2["tool_calls"]:
                if isinstance(tc, dict) and isinstance((tc.get("function") or {}).get("arguments"), str):
                    tc2 = dict(tc)
                    fn = dict(tc2["function"])
                    fn["arguments"] = _clip_text(fn["arguments"], _TOOL_PAYLOAD_CAP_TOKENS)
                    tc2["function"] = fn
                    new_calls.append(tc2)
                else:
                    new_calls.append(tc)
            m2["tool_calls"] = new_calls
        out.append(m2)
    return out


def _clip_summary(summary_msg: dict, budget_tokens: int) -> dict:
    content = summary_msg.get("content") if isinstance(summary_msg, dict) else None
    if not isinstance(content, str):
        return summary_msg
    body = content[len(_SUMMARY_HEADER):] if content.startswith(_SUMMARY_HEADER) else content
    if count_content_tokens(body) <= budget_tokens:
        return summary_msg
    # Head-keep here is acceptable for the rare floor; the fold path (L6) clips
    # the persisted summary tail-preserving so the newest sections survive.
    clipped = truncate_to_tokens(body, budget_tokens)
    return {**summary_msg, "content": _SUMMARY_HEADER + clipped + _TRUNC_NOTICE}


def _ensure_user_first(messages: list) -> list:
    """Guarantee ``messages[0].role == "user"`` (Anthropic rejects otherwise), so we
    never trade an overflow-400 for a "first message must be user" 400."""
    if not messages:
        return [{"role": "user", "content": "[Conversation context unavailable.]"}]
    first = messages[0]
    if isinstance(first, dict) and first.get("role") == "user":
        return messages
    return [{"role": "user", "content": "[Conversation resumed.]"}] + list(messages)


def enforce_ceiling(
    history: list | None,
    ceiling: int,
    *,
    system_reserve: int = 0,
    summary_budget_tokens: int | None = None,
) -> list:
    """Clip an assembled request so it fits under ``ceiling`` — the hard guarantee.

    Descent (each step only if still over): drop oldest whole turns → head-clip
    oversized ``tool`` results → head-clip oversized ``tool_call`` args → omit the
    remaining body to a tiny note → clip the running summary **last** to a fixed
    budget. Finally guarantees a ``user``-role first message. Never splits a
    ``tool_use``/``tool_result`` pair (it drops/omits whole turns) and is
    deterministic in ``(history, ceiling)``.
    """
    if not history or ceiling <= 0:
        return history or []
    if estimate_prompt_tokens(history, system_reserve=system_reserve) <= ceiling:
        return history

    hist = list(history)
    has_summary = _is_summary_message(hist[0])
    summary_msg = hist[0] if has_summary else None
    body = hist[1:] if has_summary else hist

    def over(b: list, s: Any) -> bool:
        msgs = ([s] if s else []) + b
        return estimate_prompt_tokens(msgs, system_reserve=system_reserve) > ceiling

    # 1) Drop oldest whole turns (keep at least the newest).
    turns = _split_turns(body)
    while len(turns) > 1 and over([m for t in turns for m in t], summary_msg):
        turns.pop(0)
    body = [m for t in turns for m in t]

    # 2) Truncate oversized tool results, then tool_call args.
    if over(body, summary_msg):
        body = _truncate_tool_payloads(body, args=False)
    if over(body, summary_msg):
        body = _truncate_tool_payloads(body, args=True)

    # 3) Omit the remaining body to a tiny note if still over.
    if over(body, summary_msg):
        body = [{"role": "user", "content": _OMIT_NOTE}]

    # 4) Clip the summary last, to a fixed deterministic budget.
    if summary_msg is not None and over(body, summary_msg):
        budget = summary_budget_tokens if summary_budget_tokens is not None else max(256, ceiling // 2)
        summary_msg = _clip_summary(summary_msg, budget)

    result = ([summary_msg] if summary_msg else []) + body
    return _ensure_user_first(result)


# ── Keep-recent boundary (L4) ──


def _row_tokens(row: dict, *, include_reasoning: bool) -> int:
    """Trace-aware token size of one DB message row, sized exactly as it expands
    onto the wire (its ``llm_messages`` trace when reasoning replay is on)."""
    wire = convert_db_messages_to_history([row], include_reasoning=include_reasoning)
    return estimate_prompt_tokens(wire)


def _row_is_turn_start(row: dict, *, include_reasoning: bool) -> bool:
    """Whether a row may legally BEGIN the retained verbatim tail.

    Never a row whose replayed trace starts with a bare ``tool`` result (that would
    orphan it from its ``tool_calls``). A ``user`` row qualifies when it actually
    reaches the wire — a row filtered out of history (``ui_only``, or a mid-turn
    message already carried by a trace) contributes nothing, so starting the tail
    at it would yield an assistant-first history, which Anthropic rejects. An
    ``agent`` row qualifies when its trace (or plain content) begins with assistant
    text.
    """
    role = row.get("role")
    if role == "user":
        return bool(
            convert_db_messages_to_history([row], include_reasoning=include_reasoning)
        )
    trace = row.get("llm_messages") if include_reasoning else None
    if trace:
        first = trace[0] if trace else None
        return isinstance(first, dict) and first.get("role") == "assistant"
    return role in ("agent", "assistant")


def find_boundary_watermark(
    messages: list[dict],
    keep_recent_tokens: int,
    keep_recent_messages: int,
    *,
    include_reasoning: bool,
    default: int,
) -> int:
    """Watermark (ordering of newest folded row) leaving a boundary-safe verbatim tail.

    ``messages`` is ascending by ordering (the newest slice up to the fold frontier).
    Walks backward accumulating trace-aware tokens until **both** ``keep_recent_tokens``
    and ``keep_recent_messages`` are satisfied, then snaps the tail start to the nearest
    turn boundary (:func:`_row_is_turn_start`) so the kept tail never begins with an
    orphan ``tool`` result. Returns ``default`` (the prior watermark) when the whole
    slice fits within the keep-recent window — i.e. there is nothing old enough to fold.
    """
    if not messages:
        return default

    tail_tokens = 0
    tail_count = 0
    tail_start_idx = None
    for i in range(len(messages) - 1, -1, -1):
        row = messages[i]
        tail_tokens += _row_tokens(row, include_reasoning=include_reasoning)
        tail_count += 1
        if (
            tail_tokens >= keep_recent_tokens
            and tail_count >= keep_recent_messages
            and _row_is_turn_start(row, include_reasoning=include_reasoning)
        ):
            tail_start_idx = i
            break

    if tail_start_idx is None or tail_start_idx <= 0:
        # Whole slice is within the keep-recent window (or the tail would start at the
        # very first row) — nothing old enough to fold; keep the prior watermark.
        return default

    prev = messages[tail_start_idx - 1]
    wm = prev.get("ordering")
    return int(wm) if wm is not None else default


async def _model_limits(conversation_id: str, conversation_storage: Any) -> tuple[int, int]:
    """``(context_window, response_reserve)`` for the conversation's active model.

    Reads only the latest agent message's stamped ``provider``/``model`` (no history
    assembly, so it is safe to call from :func:`build_compacted_history` without
    recursion). Falls back to :data:`DEFAULT_CONTEXT_WINDOW` on any gap.
    """
    provider = model = None
    try:
        latest = await conversation_storage.get_latest_agent_message(conversation_id)
        if latest:
            meta = latest.get("metadata") or {}
            provider, model = meta.get("provider"), meta.get("model")
    except Exception:  # noqa: BLE001
        logger.debug(f"[compaction] model-limits lookup failed for conv={conversation_id}", exc_info=True)
    window = context_window_for(provider, model) or DEFAULT_CONTEXT_WINDOW
    return window, response_reserve_for(provider, model)


async def _ceiling_for_conversation(conversation_id: str, conversation_storage: Any) -> int:
    """``context_window − response_reserve`` for the conversation's active model."""
    window, reserve = await _model_limits(conversation_id, conversation_storage)
    return max(1, window - reserve)


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
    """Return the effective history (running summary + verbatim tail), clamped to fit.

    Applies the stored compaction state (summary + frontier-anchored tail) when
    compaction is enabled; it does NOT fold automatically — that is model-driven via
    the ``compact_conversation`` tool (manual) or ``run_model_fold`` (auto).

    Then applies the deterministic emergency floor (:func:`enforce_ceiling`)
    **unconditionally** — even when compaction is disabled or errored — so the
    assembled prompt can never exceed ``window − response_reserve``. This is the hard
    guarantee that closes the overflow cliff for UI-less clients.

    ``exclude_message_id`` is the current turn's just-persisted message: the agent
    receives that turn as its volatile input, so it must be dropped from the tail or
    it would appear twice.
    """
    effective = fallback_history

    cfg = None
    try:
        cfg = resolve_compaction_config(profile)
    except Exception:  # noqa: BLE001
        logger.exception(f"[compaction] could not resolve config for profile={profile}")

    if cfg is not None and cfg.enabled:
        try:
            include_reasoning = replay_reasoning_enabled(profile)
            summary, watermark, _ = await conversation_storage.get_compaction_state(conversation_id)
            tail = await conversation_storage.get_messages_after(
                conversation_id, watermark, newest_first=True,
            )
            if exclude_message_id:
                tail = [m for m in tail if m.get("id") != exclude_message_id]
            effective = _build_effective(summary, tail, include_reasoning=include_reasoning)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[compaction] failed for conv={conversation_id}; falling back to raw history"
            )
            effective = fallback_history

    # Deterministic emergency floor — unconditional (needs no summary or model).
    try:
        ceiling = await _ceiling_for_conversation(conversation_id, conversation_storage)
        effective = enforce_ceiling(
            effective, ceiling, system_reserve=_SYSTEM_RESERVE_TOKENS,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[compaction] ceiling enforcement failed for conv={conversation_id}; "
            "returning unclamped history"
        )

    return effective


async def context_usage(
    *,
    conversation_id: str,
    profile: str,
    conversation_storage: Any,
    history: list | None = None,
) -> dict:
    """Return ``{current_tokens, context_window, threshold, response_reserve, ceiling}``.

    - ``current_tokens``: the **trace-aware** size of the assembled prompt
      (:func:`estimate_prompt_tokens` over ``[summary] + tail``, native reasoning
      traces included), cross-checked against the model's reported prompt size and
      taken as the max so it never under-reports and is never ``0`` for event-run
      rows that carry no ``context_tokens``. Pass ``history`` to size an
      already-assembled request (pre-flight / post-turn); otherwise it is assembled
      on demand (the ``/memory`` endpoint).
    - ``context_window``: the active model's context window from the catalog (via the
      latest agent message's stamped ``provider``/``model``); falls back to
      :data:`DEFAULT_CONTEXT_WINDOW`.
    - ``threshold``: ``round(compact_threshold_percent / 100 * context_window)`` — the
      *suggest* band (default 85%).
    - ``response_reserve`` / ``ceiling``: tokens held back for the reply, and the hard
      cap ``context_window − response_reserve`` the deterministic floor enforces.

    Single source of truth shared by the chat banner, the ``/memory`` endpoint, the
    pre-flight check and the floor. Defensive — never raises; falls back to safe defaults.
    """
    try:
        cfg = resolve_compaction_config(profile)
        percent = cfg.compact_threshold_percent
    except Exception:  # noqa: BLE001
        percent = 85.0

    reported = 0
    provider = model = None
    try:
        latest = await conversation_storage.get_latest_agent_message(conversation_id)
        if latest:
            tu = latest.get("token_usage") or {}
            reported = int(tu.get("context_tokens") or 0)
            meta = latest.get("metadata") or {}
            provider, model = meta.get("provider"), meta.get("model")
    except Exception:  # noqa: BLE001
        logger.debug(f"[compaction] could not read latest message for conv={conversation_id}",
                     exc_info=True)

    window = context_window_for(provider, model) or DEFAULT_CONTEXT_WINDOW
    reserve = response_reserve_for(provider, model)
    ceiling = max(1, window - reserve)

    estimate = 0
    try:
        if history is None:
            history = await build_compacted_history(
                conversation_id=conversation_id,
                profile=profile,
                conversation_storage=conversation_storage,
                fallback_history=[],
            )
        # No system_reserve here: `reported` (the model's actual last prompt size)
        # already includes the system + tools, and the history estimate is the
        # fallback when there is no reported size (event runs / first turn). Padding
        # both with a fixed reserve would over-report an otherwise-empty conversation.
        estimate = estimate_prompt_tokens(history, system_reserve=0)
    except Exception:  # noqa: BLE001
        logger.debug(f"[compaction] could not estimate size for conv={conversation_id}",
                     exc_info=True)

    current = max(estimate, reported)
    threshold = max(1, round(percent / 100 * window))
    return {
        "current_tokens": current,
        "context_window": window,
        "threshold": threshold,
        "response_reserve": reserve,
        "ceiling": ceiling,
    }


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


# ── Automatic model fold (L2) + anti-thrash (L5) ──

_AUTO_OFFSET_PERCENT = 7          # auto-fold band sits this far above the suggest threshold
_AUTO_FOLD_COOLDOWN_SEC = 60      # min seconds between auto-folds (bypassed at/over the ceiling)
_AUTO_FOLD_TIMEOUT_SEC = 120      # hard cap on a single auto-fold turn


def _auto_fold_threshold(window: int, suggest_percent: float, ceiling: int) -> int:
    """Token band above which auto-fold fires — strictly between the suggest threshold
    and the hard ceiling, so it can never invert the suggestion band."""
    suggest = round(suggest_percent / 100 * window)
    auto = round((suggest_percent + _AUTO_OFFSET_PERCENT) / 100 * window)
    auto = min(auto, max(1, ceiling - 1))
    return max(auto, suggest + 1)


async def run_model_fold(
    agent: Any, conversation_id: str, profile: str, conversation_storage: Any,
    *, context_id: str | None = None,
) -> bool:
    """Run the model-driven fold and report whether it applied.

    The **main model** writes the running summary from its already-warm cached prefix
    via the ``compact_conversation`` tool — no separate summarizer call. Shared by the
    manual "compact now" endpoint and the automatic post-turn fold. Returns ``True``
    when the compaction state actually changed (summary text or watermark advanced);
    an empty/refused summary leaves state untouched and returns ``False``. The turn's
    own token cost is billed back to the conversation (see :func:`_record_fold_usage`).

    The nested turn is an INTERNAL MAINTENANCE turn, not a turn of the
    conversation: it borrows the conversation's history, ``context_id`` and
    profile but represents nobody and nothing streams it. Both halves of that
    identity are set here — ``maintenance=True`` withholds the tools that would
    let it speak to a room or a channel on the user's behalf, and the cleared run
    binding below keeps it out of the inboxes belonging to the turn it runs
    inside. Without them a fold looks exactly like a plain chat turn, because
    every other identity signal (``message_origin``, ``event_run``) is absent.
    """
    try:
        cfg = resolve_compaction_config(profile)
        budget = cfg.max_tokens
    except Exception:  # noqa: BLE001
        budget = 2048

    before = await conversation_storage.get_compaction_state(conversation_id)
    history = await build_compacted_history(
        conversation_id=conversation_id, profile=profile,
        conversation_storage=conversation_storage, fallback_history=[],
    )
    synthetic = (
        "Please compact our conversation now to free up context. Write a dense, "
        "self-contained running summary that UPDATES the existing summary — preserve "
        "all prior facts, identifiers, decisions and pending TODOs, and move completed "
        f"items to Done — within about {budget} tokens, then call the compact_conversation "
        "tool with that summary (plus any durable long_term_memories). Do not do anything else."
    )
    fold_records: list[dict] = []
    # Run the nested turn OUTSIDE the caller's run binding. The auto path is
    # invoked from ``after_turn_compaction`` at the tail of a live turn, still
    # inside that turn's ``current_task_id_var`` and its ``bind_run``; the agent
    # loop drains the conversation's user inbox for whatever run id that var
    # names at the top of EVERY step, and nothing else distinguishes a fold's
    # loop from a real one. So a message the user sent between the outer turn's
    # last drain and its ``unbind_run`` would be handed to the FOLD: acknowledged
    # by a real LLM call streamed into a generator that keeps only the usage
    # records (so the user never sees the reply their message produced), billed
    # to "compaction", folded into the text the summary is written from — and a
    # model handed a live question plus a request to answer it may answer instead
    # of calling ``compact_conversation``, which for a seat means no fold at all
    # and a drop back to the deterministic floor that DISCARDS oldest turns.
    # Clearing the var leaves the message parked for the turn-end flush that
    # actually owns it: both drains resolve no conversation and return empty, and
    # ``get_event_task_results`` reports "no live turn" instead of consuming rows
    # into a run nobody reads. ``maintenance=True`` says the same thing where it
    # is readable and testable; this is the mechanism, because the drain keys on
    # the context var.
    ctx_token = current_task_id_var.set(None)
    try:
        async for chunk in agent.run(
            query=synthetic,
            task_history=history,
            context_id=context_id or conversation_id,
            profile=profile,
            reasoning=True,
            maintenance=True,
        ):
            # The terminal DONE chunk carries the turn's per-call usage records
            # (see ReasoningAgent._token_fields); every other chunk is UI
            # streaming this caller has no client for.
            if isinstance(chunk, dict) and chunk.get("type") is ChatCompletionTypeEnum.DONE:
                fold_records = list(chunk.get("usage_records") or [])
    finally:
        current_task_id_var.reset(ctx_token)

    await _record_fold_usage(
        records=fold_records, conversation_id=conversation_id, profile=profile,
    )

    after = await conversation_storage.get_compaction_state(conversation_id)
    return (after[0] != before[0]) or (after[1] != before[1])


async def _record_fold_usage(
    *, records: list[dict], conversation_id: str, profile: str,
) -> None:
    """Bill the fold's own LLM calls to the conversation as ``compaction`` usage.

    The fold drives a real agent turn, but its product is a tool call rather than a
    reply, so it never reaches the ``stream_runner`` accounting path and there is no
    assistant row to key the rows to — the one LLM call in the system nobody paid for
    on paper, and the one that fires hardest precisely on the conversations that cost
    the most. Records arrive stamped ``reasoning``/``tool``; rewriting them to a
    distinct ``source_kind`` keeps the dashboard from reading a fold as another
    reasoning step of the turn that triggered it. Best-effort — accounting must never
    fail a fold (the summary is already persisted by the time we get here).
    """
    if not records:
        return
    try:
        from app.storage import get_usage_storage

        rewritten = [
            {**r, "source_kind": "compaction", "label": "Compaction"} for r in records
        ]
        await get_usage_storage().add_usage_records(
            conversation_id=conversation_id,
            profile=profile,
            records=rewritten,
            message_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[compaction] failed to record fold usage for conv={conversation_id}"
        )


async def after_turn_compaction(
    agent: Any, conversation_id: str, profile: str, conversation_storage: Any,
    *, context_id: str | None = None, force_auto: bool = False,
) -> dict | None:
    """Post-turn compaction step — returns an event ``{"type", "data"}`` to publish, or ``None``.

    When ``auto_compact_enabled`` and context crosses the derived auto band, folds
    automatically via :func:`run_model_fold` (bounded by a timeout, cancel-safe, with a
    cooldown that is bypassed at/over the ceiling), emitting ``compaction_auto_folded``.
    Otherwise, over the suggest threshold, emits ``compaction_suggested`` (today's
    behavior). The deterministic floor guarantees safety regardless of the outcome.

    ``force_auto`` folds regardless of the ``auto_compact_enabled`` setting, for
    conversations that have no client able to act on a suggestion — a group-chat seat
    is hidden from the sidebar and the room's UI listens on the group bus, so its
    ``compaction_suggested`` reaches nobody and suggest-only silently means "never
    compact". Such a conversation would run forever on the deterministic floor, which
    DROPS the oldest turns rather than summarising them. For the same reason the
    suggestion is suppressed on this path: an event no one can click is noise, so a
    fold that did not happen returns ``None``.
    """
    try:
        cfg = resolve_compaction_config(profile)
    except Exception:  # noqa: BLE001
        return None
    if not cfg.enabled:
        return None

    try:
        usage = await context_usage(
            conversation_id=conversation_id, profile=profile,
            conversation_storage=conversation_storage,
        )
    except Exception:  # noqa: BLE001
        logger.debug(f"[compaction] usage check failed for conv={conversation_id}", exc_info=True)
        return None

    current = usage["current_tokens"]
    window = usage["context_window"]
    ceiling = usage["ceiling"]
    threshold = usage["threshold"]

    if (cfg.auto_compact_enabled or force_auto) and current >= _auto_fold_threshold(
        window, cfg.compact_threshold_percent, ceiling
    ):
        try:
            _, _, last_ms = await conversation_storage.get_compaction_state(conversation_id)
        except Exception:  # noqa: BLE001
            last_ms = None
        over_ceiling = current >= ceiling
        cooling = bool(last_ms) and (time.time() * 1000 - last_ms) < (_AUTO_FOLD_COOLDOWN_SEC * 1000)
        if over_ceiling or not cooling:
            folded = False
            try:
                folded = await asyncio.wait_for(
                    run_model_fold(
                        agent, conversation_id, profile, conversation_storage,
                        context_id=context_id,
                    ),
                    timeout=_AUTO_FOLD_TIMEOUT_SEC,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.info(
                    f"[compaction] auto-fold timed out/cancelled for conv={conversation_id}; "
                    "the deterministic floor still guarantees safety"
                )
            except Exception:  # noqa: BLE001
                logger.exception(f"[compaction] auto-fold failed for conv={conversation_id}")
            if folded:
                after_cur = current
                try:
                    after = await context_usage(
                        conversation_id=conversation_id, profile=profile,
                        conversation_storage=conversation_storage,
                    )
                    after_cur = after["current_tokens"]
                except Exception:  # noqa: BLE001
                    pass
                return {
                    "type": "compaction_auto_folded",
                    "data": {
                        "current_tokens": after_cur,
                        "folded_tokens": max(0, current - after_cur),
                        "context_window": window,
                    },
                }

    if current >= threshold and not force_auto:
        savings = max(0, current - cfg.keep_recent_tokens - cfg.max_tokens)
        return {
            "type": "compaction_suggested",
            "data": {
                "current_tokens": current,
                "threshold": threshold,
                "estimated_savings": savings,
                "context_window": window,
            },
        }
    return None


async def apply_compaction(
    *,
    conversation_id: str,
    profile: str,
    summary: str,
    long_term: list[str] | None,
    conversation_storage: Any,
    watermark: int | None = None,
) -> dict:
    """Persist a model-generated running summary + any long-term facts.

    Called by the ``compact_conversation`` tool's ``run()``. The watermark advances
    so the summary now covers everything up to a keep-recent boundary and the older
    turns collapse on the next turn (recent turns stay verbatim — see L4).

    ``watermark`` pins the fold frontier. The auto-fold path passes a snapshot
    captured **before** the current turn's messages were persisted, so an interleaved
    message can't be buried below the watermark. When omitted, the manual path
    defaults it to the live :meth:`get_max_ordering` frontier.

    An empty / refused summary is a **no-op**: advancing the watermark without a
    covering summary would silently drop those messages, so the prior state is left
    untouched and the caller falls back to the deterministic floor.

    Serialized per conversation so a manual and an auto fold can't race the
    read-modify-write.
    """
    async with _compaction_lock(conversation_id):
        return await _apply_compaction_locked(
            conversation_id=conversation_id, profile=profile, summary=summary,
            long_term=long_term, conversation_storage=conversation_storage,
            watermark=watermark,
        )


async def _apply_compaction_locked(
    *,
    conversation_id: str,
    profile: str,
    summary: str,
    long_term: list[str] | None,
    conversation_storage: Any,
    watermark: int | None = None,
) -> dict:
    cfg = resolve_compaction_config(profile)
    summary_old, watermark_old, _ = await conversation_storage.get_compaction_state(conversation_id)

    summary = (summary or "").strip()
    if not summary:
        logger.warning(
            f"[compaction] empty/refused summary for conv={conversation_id}; "
            "skipping fold (watermark NOT advanced)"
        )
        stored = await _store_long_term_facts(profile, conversation_id, long_term or [])
        return {
            "watermark": watermark_old,
            "summary_chars": len(summary_old or ""),
            "long_term_stored": stored,
            "skipped": "empty_summary",
        }

    # Frontier for this fold: the caller's snapshot (auto-fold — excludes the current
    # turn) or the live max(ordering) frontier (manual).
    if watermark is None:
        frontier = await conversation_storage.get_max_ordering(conversation_id)
    else:
        frontier = int(watermark)

    # L5 — clamp keep-recent so a fold lands at/below fold_target*window (summary +
    # tail + reply reserve + system reserve). This guarantees the next turn sits below
    # the deterministic floor's ceiling, so the floor never fires right after a fold.
    window, reserve = await _model_limits(conversation_id, conversation_storage)
    keep_recent_tokens = cfg.keep_recent_tokens
    fold_target = round(cfg.fold_target_percent / 100 * window)
    tail_budget = fold_target - cfg.max_tokens - reserve - _SYSTEM_RESERVE_TOKENS
    if tail_budget > 0:
        keep_recent_tokens = min(keep_recent_tokens, tail_budget)
    keep_recent_tokens = max(0, keep_recent_tokens)

    # L4 — leave a boundary-safe verbatim tail of ~keep_recent behind the frontier
    # instead of folding everything. Fold only rows at/below the computed boundary.
    include_reasoning = replay_reasoning_enabled(profile)
    slice_limit = max(500, cfg.keep_recent_messages * 5)
    recent = await conversation_storage.get_messages_after(
        conversation_id, -1, limit=slice_limit, newest_first=True,
    )
    recent = [
        m for m in recent
        if m.get("ordering") is not None and int(m["ordering"]) <= frontier
    ]
    watermark = find_boundary_watermark(
        recent, keep_recent_tokens, cfg.keep_recent_messages,
        include_reasoning=include_reasoning, default=watermark_old,
    )
    watermark = max(int(watermark), watermark_old)  # never move the watermark backward

    # L6 — clip an over-budget summary tail-preserving (keep the newest sections),
    # not head-first. The fold prompt states the budget so this rarely fires; log it.
    new_summary = summary
    if count_content_tokens(summary) > cfg.max_tokens:
        new_summary = truncate_to_tokens_tail(summary, cfg.max_tokens)
        logger.info(
            f"[compaction] summary over budget for conv={conversation_id} "
            f"({count_content_tokens(summary)}>{cfg.max_tokens} tok); clipped tail-preserving"
        )
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
