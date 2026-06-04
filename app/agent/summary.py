"""Shared reasoning-trace summarizer used by the A2A executor and the event runner."""

from __future__ import annotations

from typing import Any, cast

from app.agent.agent import CremindAgent
from app.config.user_config import resolve_summarizer_config
from app.constants import ChatCompletionTypeEnum
from app.utils.logger import logger


_SYSTEM = (
    "You summarize an agent's ReAct reasoning trace for AI-assisted agent introspection and debugging. "
    "Output concise GitHub-flavored Markdown. Describe what the agent considered, which tools it used, "
    "and what it concluded (even including the technical details like process IDs, file paths, API calls, etc). "
    "Do not invent facts beyond the trace."
)


async def summarize_reasoning(
    cremind_agent: CremindAgent | None, input_section: str, profile: str,
) -> str:
    """Produce a Markdown TL;DR of a completed ReAct trace via the low model group."""
    logger.debug("input_section for reasoning summary: " + input_section)
    if not cremind_agent:
        return ""
    llm = cremind_agent.low_group_llm(profile)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": input_section},
    ]
    cfg = resolve_summarizer_config(profile)
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
