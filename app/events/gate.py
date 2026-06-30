"""Skill-event matching gate.

A lightweight pre-filter that runs ONE cheap-model classification before a skill
event reaches the Reasoning Agent. It answers a single question: does this event's
content satisfy the conditions stated in the subscription's natural-language
``action``?

Why this exists
---------------
A skill declares only coarse events (e.g. gmail's ``new_email``). The fine-grained
condition the user actually wants ("…from li@olli-ai.com") lives only inside the
free-text ``action``. Without a gate, every matching event runs the full Reasoning
Agent, which then writes a misleading reply for events that never should have fired —
polluting history and burning a full agent turn (system prompt + tools + history).

The gate is deliberately frugal: no system prompt of the agent, no tool list, no
chat history — just the action + the event payload and a single structured decision.
It runs on the **low-performance model** (see ``CremindAgent.low_performance_llm``).

Contract
--------
- Decision is returned via **tool calling** (``report_match(matches, reason)``) so the
  output is structurally guaranteed rather than parsed from free-form text.
- Fail-open: if the model emits no tool call or an unparseable decision, the gate
  returns ``matched=True`` so a real event is never silently dropped. The caller also
  treats any raised exception as a match.
- Token usage from the call is returned so the caller can attribute its cost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.constants import ChatCompletionTypeEnum
from app.utils.logger import logger

_TOOL_NAME = "report_match"

_TOKEN_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)

_GATE_SYSTEM_PROMPT = (
    "You are an event filter for an automation system. The user subscribed to a "
    "coarse event and wrote an ACTION describing what to do when it fires. You are "
    "given that ACTION and the CONTENT of one event that just fired. Decide whether "
    "the event CONTENT satisfies the conditions stated in the ACTION.\n\n"
    "Rules:\n"
    "- Judge ONLY the conditions in the ACTION (e.g. a specific sender, subject "
    "keyword, label). Do NOT perform the action or reason beyond matching.\n"
    "- If the ACTION states no filtering condition (e.g. 'notify me of any new "
    "email'), every event matches — return matches=true.\n"
    "- When you are uncertain, prefer matches=true. It is far worse to drop a real "
    "event than to let an extra one through.\n"
    "Report your decision by calling the report_match function."
)


@dataclass
class GateResult:
    """Outcome of one gate classification."""

    matched: bool
    reason: str
    tokens: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in _TOKEN_KEYS})


def _build_gate_tools() -> List[Dict[str, Any]]:
    """OpenAI-style single-function schema for the gate's structured decision."""
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": (
                    "Report whether the event content matches the conditions in "
                    "the user's action."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "matches": {
                            "type": "boolean",
                            "description": (
                                "true if the event satisfies the action's "
                                "conditions (or the action has no condition); "
                                "false only when a stated condition is clearly "
                                "not met."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "One short sentence explaining the decision, "
                                "citing the relevant condition and event field."
                            ),
                        },
                    },
                    "required": ["matches", "reason"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _format_gate_prompt(event_type: str, action: str, file_content: str) -> str:
    return (
        f"EVENT TYPE: {event_type}\n\n"
        f"ACTION (what the user wants when this event fires):\n{action.strip()}\n\n"
        f"EVENT CONTENT (the event that just fired):\n{file_content.strip()}\n\n"
        f"Call {_TOOL_NAME} with your decision."
    )


def _coerce_bool(value: Any) -> bool | None:
    """Coerce a tool-call ``matches`` value to a bool, or ``None`` if ambiguous.

    Returning ``None`` for anything unrecognized is load-bearing: the caller
    treats ``None`` as fail-open (match). Only explicit true/false-like values
    decide; a junk string (``""``, ``"maybe"``, ``"n/a"``) must NOT silently
    coerce to False and drop a real event.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            return True
        if v in {"false", "0", "no", "off"}:
            return False
        return None  # unrecognized → fail open at the call site
    return None


async def classify_event_match(
    *,
    llm,
    event_type: str,
    action: str,
    file_content: str,
) -> GateResult:
    """Decide whether ``file_content`` satisfies ``action`` for ``event_type``.

    Runs one structured tool-calling completion on ``llm`` (the low-performance
    model). Fail-open: any missing/unparseable decision yields ``matched=True``.
    Raising propagates to the caller, which also fail-opens.
    """
    tools = _build_gate_tools()
    messages = [
        {"role": "system", "content": _GATE_SYSTEM_PROMPT},
        {"role": "user", "content": _format_gate_prompt(event_type, action, file_content)},
    ]

    function_calls: List[Dict[str, Any]] = []
    tokens: Dict[str, int] = {k: 0 for k in _TOKEN_KEYS}

    # tool_choice="auto" mirrors the documentation_search judge (works across all
    # configured providers); the lone tool + strong instruction make a call the
    # overwhelming default, and the no-call branch fails open anyway.
    async for response in llm.chat_completion(
        messages=messages,
        tools=tools,
        tool_choice="auto",
    ):
        rtype = response.get("type")
        if rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
            data = response.get("data")
            if isinstance(data, dict) and data.get("function"):
                function_calls = data["function"]
        elif rtype == ChatCompletionTypeEnum.DONE:
            for k in _TOKEN_KEYS:
                tokens[k] = int(response.get(k) or 0)
            break

    if not function_calls:
        logger.warning(
            "[event_gate] model produced no decision tool call; failing open (match)"
        )
        return GateResult(True, "no decision returned; defaulted to match", tokens)

    call = function_calls[0]
    args = call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}

    matched = _coerce_bool(args.get("matches") if isinstance(args, dict) else None)
    reason = str(args.get("reason") if isinstance(args, dict) else "").strip()

    if matched is None:
        logger.warning("[event_gate] unparseable 'matches' value; failing open (match)")
        return GateResult(True, reason or "ambiguous decision; defaulted to match", tokens)

    return GateResult(matched, reason or ("matched" if matched else "did not match"), tokens)
