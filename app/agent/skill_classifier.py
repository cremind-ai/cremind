"""Classify a skill invocation as either ``load`` (one-shot) or ``event``
(register a recurring trigger).

The reasoning agent's dispatcher calls :func:`classify_skill_request` whenever
the LLM emits a SKILL action. The classifier runs a separate, short LLM call
to decide whether the user wants the skill loaded for immediate execution or
subscribed to a filesystem event so a saved instruction runs later.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

from app.config.user_config import resolve_skill_classifier_config
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import LLMProvider
from app.utils.logger import logger

# Stringified annotation usage only; keep the type loadable for static
# checking without forcing ``openai`` onto the boot path.
if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam


_SYSTEM_PROMPT = (
    "You classify a single user request that targets a skill into one of two "
    "categories. Return ONLY a JSON object of the shape "
    '{"response_type": "load"} or {"response_type": "event"}, with no other '
    "text.\n\n"
    "- 'load': the user wants the skill to perform an immediate, one-shot "
    "task right now (e.g. 'list my emails today', 'send an email to X', "
    "'show today's calendar').\n"
    "- 'event': the user wants something to happen automatically every time "
    "an event fires in the future (e.g. 'when a new email arrives, "
    "summarize it', 'whenever I get a calendar invite, draft a reply', "
    "'each time X happens, do Y'). Phrasings like 'when', 'whenever', "
    "'on new ...', 'each time', 'every time', 'auto-...' all signal 'event'."
)

_LOAD_INSTRUCTION = (
    "You should only follow the instructions provided in the content loaded "
    "in {skill_id} skill."
)

_EVENT_INSTRUCTION = (
    "Please use the **Register Skill Event** tool to register a skill event "
    "for this skill."
)

_EVENT_SKILL_CONTENT_BLOCK = (
    "\n\nSKILL.md content for {skill_id}:\n{skill_content}"
)


async def classify_skill_request(
    *,
    action_input: str,
    skill_id: str,
    skill_name: str,
    source: str,
    llm: LLMProvider,
    profile: str,
    skill_content: str = "",
) -> Dict[str, Any]:
    """Classify ``action_input`` as either ``load`` or ``event``.

    Returns a payload with all five fields the reasoning agent needs to route
    the request. On any LLM/parse failure, defaults to ``load`` (the safer
    fallback — the skill simply gets loaded, the same as today's behavior).

    When the verdict is ``event``, ``skill_content`` (typically the skill's
    full ``SKILL.md`` text) is appended to the returned ``instruction`` so the
    downstream ``register_skill_event`` call has full context about the skill.
    """
    user_text = (
        f"Skill: {skill_name}\n"
        f"User request: {action_input}\n\n"
        'Respond with JSON only: {"response_type":"load"} or '
        '{"response_type":"event"}.'
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]

    cfg = resolve_skill_classifier_config(profile)
    raw = ""
    try:
        async for response in llm.chat_completion(
            messages=messages,
            tools=None,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            retry=cfg.retry,
        ):
            if response["type"] == ChatCompletionTypeEnum.CONTENT:
                chunk = response.get("data") or ""
                if chunk:
                    raw += chunk
            elif response["type"] == ChatCompletionTypeEnum.DONE:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"skill_classifier: LLM call failed for skill={skill_id}: {exc}. "
            f"Defaulting to 'load'."
        )
        return _payload(skill_id, skill_name, source, "load", skill_content)

    response_type = _extract_response_type(raw)
    logger.debug(
        f"skill_classifier: skill={skill_id} response_type={response_type} "
        f"raw={raw!r}"
    )
    return _payload(skill_id, skill_name, source, response_type, skill_content)


def _extract_response_type(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "load"
    # Try JSON first (the system prompt asks for JSON).
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            value = str(data.get("response_type", "")).strip().lower()
            if value in ("load", "event"):
                return value
    except json.JSONDecodeError:
        pass
    # Fall back to substring sniffing for resilience against models that
    # forget to wrap the answer in JSON.
    lowered = text.lower()
    if '"event"' in lowered or "'event'" in lowered or lowered == "event":
        return "event"
    if '"load"' in lowered or "'load'" in lowered or lowered == "load":
        return "load"
    return "load"


def _payload(
    skill_id: str,
    skill_name: str,
    source: str,
    response_type: str,
    skill_content: str = "",
) -> Dict[str, Any]:
    if response_type == "event":
        instruction = _EVENT_INSTRUCTION
        content = (skill_content or "").strip()
        if content:
            instruction += _EVENT_SKILL_CONTENT_BLOCK.format(
                skill_id=skill_id,
                skill_content=content,
            )
    else:
        instruction = _LOAD_INSTRUCTION.format(skill_id=skill_id)
    return {
        "skill_id": skill_id,
        "skill_name": skill_name,
        "source": source,
        "response_type": response_type,
        "instruction": instruction,
    }
