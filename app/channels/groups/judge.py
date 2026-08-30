""""Is this message for me?" — one cheap call, for one agent.

Not a router. :mod:`app.groups.routing` decides which of SEVERAL agents in a
Cremind room should be woken. This judge answers a narrower question for a
single agent that is a *member* of a real group: of everything said here, which
part is mine to answer?

The framing matters, and an earlier version got it wrong. Written as "should an
assistant intrude on these people?", the judge declined everything that was not
an explicit command — including "Hello everyone, how are you?", a question
addressed to a group the agent is a member of, which every other member
answered. An account in a group chat is a participant: it answers what is put to
the room as well as what is put to it by name, and stays out only of exchanges
that are demonstrably somebody else's.

Two failure directions, deliberately treated differently:

*A wrong answer* is recoverable — it is one message in a chat, and the loop
brakes above this stop two assistants from feeding on each other. So an
ambiguous message aimed at the room resolves to *yes*.

*A broken judge* is not — a provider outage would otherwise turn every group
into a chatterbox. So every ERROR path (no tool call, unparseable arguments, a
timeout, a provider error, no LLM at all) still resolves to "not relevant". The
message is stored as context either way; only the reply is withheld.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.channels.groups.constants import JUDGE_SOURCE_KIND
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import done_chunk_token_usage
from app.utils.logger import logger

_TOOL_NAME = "report_relevance"

# Bounded because the caller holds the group's inbound lock: every later message
# from the same room waits behind this call. Twelve seconds is well past a
# low-tier model's normal latency for a short classification, and a group that
# is stuck is better served by a silent agent than by a stalled queue.
_TIMEOUT_S = 12.0

_SYSTEM_PROMPT = (
    "You decide whether one member of a group chat should reply to the newest "
    "message. That member is an AI assistant, but it is a full participant in "
    "the group: it has its own account and its own name there, and the other "
    "members talk to it the way they talk to each other.\n\n"
    "Answer relevant=true when:\n"
    "- the message is addressed to the assistant — by its agent name, by the "
    "account name it appears under in this group, or with an @mention;\n"
    "- the message is addressed to the GROUP as a whole and invites an answer: "
    "a greeting to everyone, 'anyone…?', or any question with no named "
    "addressee. The assistant is one of the people being asked, so it answers "
    "like the others would;\n"
    "- it continues an exchange the RECENT messages show the assistant is "
    "already in — a reply, a follow-up question, or a correction to something "
    "the assistant said;\n"
    "- it asks about something the assistant said, did, or knows about;\n"
    "- several members could answer and the assistant is the best placed of "
    "them.\n\n"
    "Answer relevant=false when:\n"
    "- the message is addressed to another member by name (the members are "
    "listed for you) and not also to the assistant;\n"
    "- it belongs to an exchange between two other members that the assistant "
    "was never part of;\n"
    "- it is an acknowledgement or a closing remark aimed at somebody else "
    "('thanks Sam', 'ok', 'got it');\n"
    "- the assistant has already answered this and nothing new is being asked;\n"
    "- it is another assistant's answer to the same question — two assistants "
    "replying to each other is noise, so do not join in.\n\n"
    "When a message is aimed at the group as a whole, answer relevant=true. "
    "Answer relevant=false only when the message is clearly for somebody else.\n"
    f"Report your decision by calling the {_TOOL_NAME} function."
)


@dataclass
class RelevanceResult:
    relevant: bool
    reason: str = ""
    tokens: Dict[str, int] = field(default_factory=dict)


def _build_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": (
                    "Report whether the assistant should reply to the newest "
                    "message."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "relevant": {
                            "type": "boolean",
                            "description": (
                                "True only if the assistant should post a reply "
                                "to the newest message."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "One short sentence explaining the decision."
                            ),
                        },
                    },
                    "required": ["relevant", "reason"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _coerce_bool(value: Any) -> Optional[bool]:
    """Coerce the tool call's ``relevant`` value, or ``None`` if ambiguous.

    ``None`` is distinct from ``False`` at the call site only in what it logs —
    both withhold the reply. Kept separate anyway so "the model said no" and
    "the model said something we could not read" are different lines in the log
    when somebody is working out why an agent went quiet.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return None


def _format_prompt(
    *,
    agent_name: str,
    agent_handle: str,
    account_name: str = "",
    platform_name: str,
    group_title: str,
    members: Sequence[str] = (),
    recent: Sequence[str],
    message: str,
) -> str:
    """The one user message the judge reads.

    ``account_name`` and ``members`` are what make "addressed to somebody else"
    decidable. Without the account name the judge cannot tell that "Lý Nguyen,
    what time is it?" IS the assistant being asked — it reads the account's own
    name as a third party and declines. Without the member list it cannot tell a
    name apart from an ordinary word.
    """
    names = [n for n in (agent_name, account_name) if n]
    who = " / ".join(dict.fromkeys(names)) or "the assistant"
    if agent_handle:
        who = f"{who} ({agent_handle})"
    history = "\n".join(recent) or "(nothing yet)"
    others = ", ".join(m for m in members if m) or "(not known)"
    return (
        f"ASSISTANT: {who}\n"
        f"IT APPEARS IN THIS GROUP AS: {account_name or agent_name}\n"
        f"GROUP: \"{group_title}\" on {platform_name}\n"
        f"OTHER MEMBERS: {others}\n\n"
        f"RECENT MESSAGES (oldest first):\n{history}\n\n"
        f"NEW MESSAGE:\n{message}\n\n"
        f"Call {_TOOL_NAME} with your decision."
    )


async def classify_relevance(
    *,
    llm,
    agent_name: str,
    agent_handle: str = "",
    account_name: str = "",
    platform_name: str,
    group_title: str,
    members: Sequence[str] = (),
    recent: Sequence[str],
    message: str,
) -> RelevanceResult:
    """One structured completion on the low-performance model.

    An unreadable answer resolves to "not relevant" — see the module docstring
    for why a broken judge stays quiet while an ambiguous message does not.
    Raising propagates to :func:`judge_relevance`, which does the same.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _format_prompt(
                agent_name=agent_name,
                agent_handle=agent_handle,
                account_name=account_name,
                platform_name=platform_name,
                group_title=group_title,
                members=members,
                recent=recent,
                message=message,
            ),
        },
    ]

    function_calls: List[Dict[str, Any]] = []
    tokens: Dict[str, int] = done_chunk_token_usage({})

    # tool_choice="auto" mirrors the skill-event gate — it is what works across
    # every configured provider, and the lone tool plus a direct instruction
    # make a call the overwhelming default. The no-call branch is closed anyway.
    async for response in llm.chat_completion(
        messages=messages,
        tools=_build_tools(),
        tool_choice="auto",
    ):
        rtype = response.get("type")
        if rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
            data = response.get("data")
            if isinstance(data, dict) and data.get("function"):
                function_calls = data["function"]
        elif rtype == ChatCompletionTypeEnum.DONE:
            tokens = done_chunk_token_usage(response)
            break

    if not function_calls:
        logger.info(
            "[channel_group] relevance judge returned no decision; staying quiet"
        )
        return RelevanceResult(False, "no decision returned", tokens)

    args = function_calls[0].get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    relevant = _coerce_bool(args.get("relevant"))
    reason = str(args.get("reason") or "").strip()
    if relevant is None:
        logger.info(
            "[channel_group] relevance judge gave an unreadable answer; staying quiet"
        )
        return RelevanceResult(False, reason or "ambiguous decision", tokens)
    return RelevanceResult(
        relevant, reason or ("relevant" if relevant else "not relevant"), tokens,
    )


async def judge_relevance(
    *,
    profile: str,
    agent_name: str,
    agent_handle: str,
    platform_name: str,
    group_title: str,
    recent: Sequence[str],
    message: str,
    account_name: str = "",
    members: Sequence[str] = (),
    conversation_id: Optional[str] = None,
) -> bool:
    """Resolve the profile's low-tier model and ask it. Never raises.

    ``False`` on every failure path, including no agent wired at all (tests, the
    slim CLI) — see the module docstring for why a broken judge stays quiet even
    though an ambiguous message does not.
    """
    try:
        from app.events.runner import get_cremind_agent

        agent = get_cremind_agent()
        if agent is None:
            logger.debug(
                "[channel_group] no agent wired; the relevance judge stays quiet"
            )
            return False
        llm = agent.low_performance_llm(profile)
    except Exception:  # noqa: BLE001
        logger.exception("[channel_group] relevance judge has no LLM; staying quiet")
        return False

    try:
        result = await asyncio.wait_for(
            classify_relevance(
                llm=llm,
                agent_name=agent_name,
                agent_handle=agent_handle,
                account_name=account_name,
                platform_name=platform_name,
                group_title=group_title,
                members=members,
                recent=recent,
                message=message,
            ),
            timeout=_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — timeout or provider error
        logger.exception("[channel_group] relevance judge failed; staying quiet")
        return False

    await record_judge_usage(
        llm=llm,
        tokens=result.tokens,
        profile=profile,
        group_title=group_title,
        conversation_id=conversation_id,
    )
    logger.info(
        f"[channel_group] relevance judge on \"{group_title}\": "
        f"{'reply' if result.relevant else 'stay quiet'} — {result.reason}"
    )
    return result.relevant


async def record_judge_usage(
    *,
    llm: Any,
    tokens: dict,
    profile: str,
    group_title: str,
    conversation_id: Optional[str],
) -> None:
    """Persist the judgement's LLM call. Best-effort — accounting never blocks.

    Attributed to the group's own conversation, so the cost shows up where the
    messages that caused it are.
    """
    if llm is None or not tokens or not any(tokens.values()):
        return
    try:
        from app.agent.usage import UsageRecord
        from app.storage import get_usage_storage

        record = UsageRecord(
            source_kind=JUDGE_SOURCE_KIND,
            tool_id=None,
            label=f"Group relevance: {group_title}"[:200],
            provider=getattr(llm, "provider_name", None),
            model=getattr(llm, "model_name", None),
            model_group=None,
            step_index=0,
            input_tokens=int(tokens.get("input_tokens") or 0),
            cache_read_input_tokens=int(tokens.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(
                tokens.get("cache_creation_input_tokens") or 0
            ),
            output_tokens=int(tokens.get("output_tokens") or 0),
        )
        await get_usage_storage().add_usage_records(
            conversation_id=conversation_id,
            profile=profile,
            records=[record.to_dict()],
            message_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[channel_group] failed to record the judge's usage")
