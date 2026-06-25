"""LLM-driven extraction of the running summary + long-term memory at fold time.

This is the **memory-enabled** branch of the unified compaction/memory pass (see
:mod:`app.agent.compaction`). When memory is enabled, the fold is a single
**forced tool call**: the model must call ``save_memory`` with

- ``short_term_memory`` — the merged RUNNING SUMMARY (prior summary + the older
  messages being folded out of context). This *is* the conversation compact;
  compaction stores it as the conversation's running summary.
- ``long_term_memories`` — 0+ durable, session-independent facts. The extraction
  instruction is STRICT when long-term lives in the size-limited DB queue
  (embedding off) and FLEXIBLE when it lives in the effectively-unlimited vector
  store (embedding on).

When memory is *disabled*, compaction skips this module entirely and produces the
summary with a plain chat completion (``compaction._summarize_plain``).

Tool-call results stream back as a ``FUNCTION_CALLING`` event whose
``data.function[0].arguments`` the provider has already parsed (we still defend
against a JSON-string fallback, matching :mod:`app.tools.builtin.adapter`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import LLMProvider
from app.utils.logger import logger

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam


# The short-term field is the running summary; its instruction merges the
# continuity-summary guidance that used to live in ``compaction._SUMMARY_SYSTEM``.
_SHORT_TERM_INSTRUCTION = (
    "SHORT-TERM memory (REQUIRED) — the RUNNING SUMMARY: you maintain a running "
    "summary of a long conversation so the agent keeps full continuity after older "
    "turns are dropped from its context window. You are given the PRIOR SUMMARY "
    "(already covering the earliest part of the conversation) and a batch of OLDER "
    "MESSAGES that now need to be folded in. Produce ONE merged summary that "
    "supersedes the prior summary and absorbs the new messages. Preserve, densely "
    "and specifically: facts and decisions; identifiers (IDs, file paths, URLs, "
    "ticket/PR numbers, command names, config keys, exact values); unresolved "
    "questions and pending TODOs; the user's stated goals, constraints, and "
    "preferences; and any state the agent must not re-derive. Carry forward "
    "everything still relevant from the prior summary — do not drop it just because "
    "it is old. Do not invent anything not present in the inputs. Output "
    "GitHub-flavored Markdown, with no preamble. Keep it under {summary_max} tokens."
)

# STRICT: long-term lives in a size-capped DB queue, so only the most durable facts.
_LONG_TERM_STRICT = (
    "LONG-TERM memory (OPTIONAL — usually empty): only EXTREMELY important, durable "
    "facts about the user that persist far beyond this session and rarely change — "
    "e.g. their name, age, role, stable preferences. Each entry must be a single "
    "short fact under {long_max} tokens. DO NOT store transient or session-bound "
    "information here: no reminders, schedules, calendar events, to-dos, or anything "
    "tied to the current task. If nothing qualifies, return an empty list."
)

# FLEXIBLE: long-term lives in the vector store (effectively unlimited), so capture
# any globally reusable knowledge, not just rare biographical facts.
_LONG_TERM_FLEXIBLE = (
    "LONG-TERM memory (OPTIONAL): capture any GLOBALLY useful, reusable knowledge "
    "that would help in FUTURE, unrelated conversations — not just rare biographical "
    "facts. Include durable user facts and preferences; stable project/environment "
    "details (repositories, paths, services, conventions, where credentials live — "
    "never the secret values themselves); recurring workflows and how the user likes "
    "them done; and decisions plus their rationale that outlive the current task. "
    "Each entry must be a single, self-contained fact (it will be retrieved out of "
    "context) under {long_max} tokens. Exclude only truly ephemeral state (one-off "
    "reminders, the current to-do). Storage is effectively unlimited — err toward "
    "capturing reusable knowledge. If nothing qualifies, return an empty list."
)

_SYSTEM_PROMPT_TEMPLATE = (
    "You maintain an AI agent's MEMORY by distilling a slice of a long conversation "
    "into two kinds of memory. You MUST call the `save_memory` tool exactly once.\n\n"
    "{short_term}\n\n"
    "{long_term}\n\n"
    "DEDUPLICATION: the agent's CURRENT long-term memory is given below. Do NOT "
    "repeat facts that are already stored; only add genuinely new information."
)

_SAVE_MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "Persist the running summary and any durable long-term facts extracted "
            "from this fold. Always provide short_term_memory (the merged running "
            "summary); provide long_term_memories only for durable, reusable facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "short_term_memory": {
                    "type": "string",
                    "description": (
                        "REQUIRED. The merged RUNNING SUMMARY: the prior summary "
                        "updated to absorb the folded older messages, preserving "
                        "facts, identifiers, TODOs, goals and constraints."
                    ),
                },
                "long_term_memories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "0+ durable, session-independent facts, each a single "
                        "self-contained fact. Empty if none qualify."
                    ),
                },
            },
            "required": ["short_term_memory"],
        },
    },
}


def _bullets(items: list[str]) -> str:
    items = [i for i in (s.strip() for s in items) if i]
    return "\n".join(f"- {i}" for i in items) if items else "(none)"


def _parse_save_memory(arguments: Any) -> dict[str, Any] | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    short_term = str(arguments.get("short_term_memory") or "").strip()
    raw_long = arguments.get("long_term_memories") or []
    long_term: list[str] = []
    if isinstance(raw_long, list):
        for item in raw_long:
            text = str(item).strip()
            if text:
                long_term.append(text)
    elif isinstance(raw_long, str) and raw_long.strip():
        long_term.append(raw_long.strip())
    if not short_term and not long_term:
        return None
    return {"short_term_memory": short_term, "long_term_memories": long_term}


async def extract_fold_memory(
    *,
    llm: LLMProvider,
    fold_input: str,
    long_term_flexible: bool,
    current_long_term: list[str],
    summary_max_tokens: int,
    long_max_tokens: int,
    temperature: float,
    max_tokens: int,
    retry: int,
) -> dict[str, Any] | None:
    """Run the unified fold extraction (forced ``save_memory`` tool call).

    ``fold_input`` is the rendered "PRIOR SUMMARY + OLDER MESSAGES" block (built by
    :func:`app.agent.compaction._render_fold_input`). ``long_term_flexible``
    selects the FLEXIBLE (vector-store) vs STRICT (DB-queue) long-term prompt.

    Returns ``{short_term_memory, long_term_memories}`` or ``None`` when the call
    failed or produced nothing usable — the caller treats ``None`` as a no-op and
    leaves the running summary/watermark unchanged.
    """
    if not fold_input.strip():
        return None

    long_term_block = (_LONG_TERM_FLEXIBLE if long_term_flexible else _LONG_TERM_STRICT).format(
        long_max=long_max_tokens
    )
    system = _SYSTEM_PROMPT_TEMPLATE.format(
        short_term=_SHORT_TERM_INSTRUCTION.format(summary_max=summary_max_tokens),
        long_term=long_term_block,
    )
    user_content = (
        "CURRENT long-term memory (this profile):\n"
        f"{_bullets(current_long_term)}\n\n"
        f"{fold_input}\n\n"
        "Call save_memory now."
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    function_calls: list[dict[str, Any]] = []
    try:
        async for response in llm.chat_completion(
            messages=messages,  # type: ignore[arg-type]
            tools=[_SAVE_MEMORY_TOOL],  # type: ignore[list-item]
            tool_choice={"type": "function", "function": {"name": "save_memory"}},
            temperature=temperature,
            max_tokens=max_tokens,
            retry=retry,
        ):
            rtype = response.get("type")
            if rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
                data = response.get("data")
                if isinstance(data, dict) and data.get("function"):
                    function_calls = data["function"]
            elif rtype == ChatCompletionTypeEnum.DONE:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory] fold extraction LLM call failed: {exc}")
        return None

    for call in function_calls:
        if call.get("name") == "save_memory":
            parsed = _parse_save_memory(call.get("arguments"))
            if parsed is not None:
                return parsed
    logger.debug("[memory] fold extraction produced no usable save_memory call")
    return None
