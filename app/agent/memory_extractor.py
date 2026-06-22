"""LLM-driven extraction of short/long-term memory from a conversation window.

Runs as part of the background "memory session" (see
:mod:`app.agent.memory_runner`), entirely decoupled from the reasoning loop.
It mirrors the auxiliary-LLM-call shape of :mod:`app.agent.summary` /
:mod:`app.agent.skill_classifier`, but uses a **forced tool call**: the model
must call ``save_memory`` with a required short-term summary and an optional
list of long-term facts. Tool-call results stream back as a
``FUNCTION_CALLING`` event whose ``data.function[0].arguments`` the provider
has already parsed (we still defend against a JSON-string fallback, matching
:mod:`app.tools.builtin.adapter`).

Current memory is passed into the prompt so the model dedups — it should not
re-summarize facts already captured.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.config.user_config import MemoryConfig
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import LLMProvider
from app.utils.logger import logger

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam


_SYSTEM_PROMPT = (
    "You maintain an AI agent's MEMORY by distilling a slice of a conversation "
    "into two kinds of memory. You MUST call the `save_memory` tool exactly once.\n\n"
    "SHORT-TERM memory (REQUIRED): a concise summary of THIS working session that "
    "will help the agent optimize the requests that follow — mistakes it made and "
    "should avoid repeating, commands/tools it ran repeatedly, and the user's "
    "commanding habits and preferences within this session. Write it as dense, "
    "actionable notes. Keep it under {short_max} tokens.\n\n"
    "LONG-TERM memory (OPTIONAL — usually empty): only EXTREMELY important, durable "
    "facts about the user that persist far beyond this session and rarely change — "
    "e.g. their name, age, role, stable preferences. Each entry must be a single "
    "short fact under {long_max} tokens. DO NOT store transient or session-bound "
    "information here: no reminders, schedules, calendar events, to-dos, or "
    "anything tied to the current task. If nothing qualifies, return an empty list.\n\n"
    "DEDUPLICATION: the agent's CURRENT memory is given below. Do NOT repeat facts "
    "that are already stored; only add genuinely new information. For short-term, "
    "summarize what is new in this slice rather than restating prior summaries."
)

_SAVE_MEMORY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": (
            "Persist memory extracted from the conversation slice. Always provide "
            "short_term_memory; provide long_term_memories only for durable user facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "short_term_memory": {
                    "type": "string",
                    "description": (
                        "REQUIRED. Concise, actionable session notes: mistakes to "
                        "avoid, repeated commands, user habits/preferences this session."
                    ),
                },
                "long_term_memories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "0+ durable, session-independent user facts (name, age, "
                        "stable preferences), each a single short fact. Empty if none."
                    ),
                },
            },
            "required": ["short_term_memory"],
        },
    },
}


def _format_current_memory(short_term: list[str], long_term: list[str]) -> str:
    def _bullets(items: list[str]) -> str:
        items = [i for i in (s.strip() for s in items) if i]
        return "\n".join(f"- {i}" for i in items) if items else "(none)"

    return (
        "CURRENT short-term memory (this conversation):\n"
        f"{_bullets(short_term)}\n\n"
        "CURRENT long-term memory (this profile):\n"
        f"{_bullets(long_term)}"
    )


def _format_window(window_messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in window_messages:
        role = m.get("role", "")
        speaker = "User" if role == "user" else "Agent"
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{speaker}: {content}")
    return "\n\n".join(lines)


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


async def extract_memory(
    *,
    llm: LLMProvider,
    window_messages: list[dict[str, Any]],
    current_short_term: list[str],
    current_long_term: list[str],
    cfg: MemoryConfig,
) -> dict[str, Any] | None:
    """Run the extraction LLM call. Returns the parsed memory dict or ``None``.

    The returned dict has ``short_term_memory`` (str, possibly empty) and
    ``long_term_memories`` (list[str]). ``None`` means the call failed or the
    model produced nothing usable — the caller treats that as a no-op.
    """
    window_text = _format_window(window_messages)
    if not window_text:
        return None

    system = _SYSTEM_PROMPT.format(
        short_max=cfg.short_term_max_tokens, long_max=cfg.long_term_max_tokens
    )
    user_content = (
        f"{_format_current_memory(current_short_term, current_long_term)}\n\n"
        "NEW conversation slice to extract memory from:\n"
        "----------------------------------------\n"
        f"{window_text}\n"
        "----------------------------------------\n\n"
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
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            retry=cfg.retry,
        ):
            rtype = response.get("type")
            if rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
                data = response.get("data")
                if isinstance(data, dict) and data.get("function"):
                    function_calls = data["function"]
            elif rtype == ChatCompletionTypeEnum.DONE:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory] extraction LLM call failed: {exc}")
        return None

    for call in function_calls:
        if call.get("name") == "save_memory":
            parsed = _parse_save_memory(call.get("arguments"))
            if parsed is not None:
                return parsed
    logger.debug("[memory] extraction produced no usable save_memory call")
    return None
