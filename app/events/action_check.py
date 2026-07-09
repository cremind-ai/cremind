"""Self-containment gate for registered event actions.

A lightweight pre-persist check that runs ONE cheap-model classification before
an automation registration (a schedule, a file watcher, or a skill-event
subscription) is saved. It answers a single question: is the registered
``action`` self-contained — could an agent execute it later with NO other
context?

Why this exists
---------------
A registered ``action`` is frozen text executed later in a FRESH conversation
with zero memory of the registering one. If the model writes "open the provided
URL" or "email the file mentioned above" without inlining the actual URL / path,
the fired run has no way to resolve the reference — it either stalls asking the
user (defeating the automation) or guesses. This gate catches such dangling
references at registration time and asks the model to re-call with every
concrete value inlined verbatim.

It is the sibling of the skill-event matching gate (:mod:`app.events.gate`) and
shares its posture:

Contract
--------
- Decision is returned via **tool calling** (``report_action_check``) so the
  output is structurally guaranteed rather than parsed from free text.
- Fail-OPEN everywhere: an unresolved LLM, a raised exception, a timeout, or an
  unparseable decision all let the registration proceed. The worst case equals
  today's behavior (no gate); the gate must never block a registration on
  infrastructure failure.
- Always-on, no config flag — same stance as the matching gate.
- Runs on the **low-performance model** (see ``CremindAgent.low_performance_llm``).
- Token usage is recorded best-effort as an ``event_gate`` usage record,
  attributed to the registering conversation.

``request_context`` limitation
-------------------------------
Passing the registering request lets the judge catch *paraphrased* omissions
(the action says "check the candidate page" and the URL lived only in the
request). On a plan-EXECUTION turn the "current query" is the approval message
("I accept the plan…"), not the original request — so ``request_context`` is
thin there. That is acceptable: the production defect ("the **provided** URL")
is detectable from the action text alone; ``request_context`` is a bonus signal
for single-turn registrations.

Testing note
------------
The LLM is resolved lazily via ``app.events.runner.get_cremind_agent()``, which
no test wires (``set_globals`` is server-boot only). So the whole test suite
fail-opens without a live model. A test that DOES install a fake agent must
monkeypatch :func:`gate_registration_action` (or the checker) to exercise a
rejection path deterministically.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import done_chunk_token_usage
from app.utils.logger import logger

_TOOL_NAME = "report_action_check"

# Cap the registering-request text folded into the judge prompt — it is only a
# hint for spotting omitted concrete values, not the thing being judged.
_REQUEST_CONTEXT_MAX_CHARS = 6000

# Upper bound on the whole check so a hung provider call fails open quickly
# instead of consuming the adapter's full tool-call timeout and surfacing as a
# spurious "registration failed".
_CHECK_TIMEOUT_S = 20.0

_CHECK_SYSTEM_PROMPT = (
    "You are a completeness checker for an automation system. The ACTION below "
    "is frozen text that will be executed later by an agent in a FRESH "
    "conversation with NO memory of the current one. Decide whether that agent "
    "could execute the ACTION as written, or whether the ACTION points at "
    "information it does not contain.\n\n"
    "Rules:\n"
    "- Flag ONLY clearly-dangling references: phrases that point outside the "
    "ACTION itself, e.g. 'the provided URL', 'the link above', 'the file I "
    "mentioned', 'the email we discussed', or naming a specific "
    "page/resource/document while omitting its address, path, or identifier.\n"
    "- If a REGISTERING REQUEST is provided and it contains a concrete value "
    "(URL, email address, file path, ID) that the ACTION clearly relies on but "
    "does not include, flag that too.\n"
    "- Do NOT flag general vagueness, missing step-by-step detail, or "
    "information the executing agent can look up with its own tools ('summarize "
    "my unread email', 'check today's calendar' are self-contained — the agent "
    "has tools).\n"
    "- When you are uncertain, prefer self_contained=true. It is far worse to "
    "block a valid registration than to let an imperfect one through.\n"
    "Report your decision by calling the report_action_check function."
)


@dataclass
class ActionCheckResult:
    """Outcome of one self-containment classification."""

    self_contained: bool
    missing: List[str] = field(default_factory=list)
    reason: str = ""
    tokens: Dict[str, int] = field(default_factory=lambda: done_chunk_token_usage({}))


def _build_check_tools() -> List[Dict[str, Any]]:
    """OpenAI-style single-function schema for the structured decision."""
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": (
                    "Report whether the automation action is self-contained "
                    "enough to execute with no other context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "self_contained": {
                            "type": "boolean",
                            "description": (
                                "true if an agent with NO other context could "
                                "execute the action as written; false ONLY when "
                                "the action clearly references information that "
                                "is pointed to but not included."
                            ),
                        },
                        "missing": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "One entry per dangling reference: quote the "
                                "phrase and say what concrete value must be "
                                "inlined (e.g. \"'the provided VietnamWorks URL' "
                                "— inline the full https:// address\"). Empty "
                                "when self_contained is true."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short sentence explaining the decision.",
                        },
                    },
                    "required": ["self_contained", "reason"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _format_check_prompt(action: str, request_context: str) -> str:
    ctx = (request_context or "").strip()
    if len(ctx) > _REQUEST_CONTEXT_MAX_CHARS:
        ctx = ctx[:_REQUEST_CONTEXT_MAX_CHARS] + " …[truncated]"
    return (
        "ACTION (frozen text that will run later with no other context):\n"
        f"{action.strip()}\n\n"
        "REGISTERING REQUEST (the message that asked for this automation; use it "
        "ONLY to spot concrete values the ACTION relies on but omits):\n"
        f"{ctx or '(not available)'}\n\n"
        f"Call {_TOOL_NAME} with your decision."
    )


def _coerce_bool(value: Any) -> Optional[bool]:
    """Coerce a tool-call ``self_contained`` value to a bool, or ``None`` if
    ambiguous.

    ``None`` is load-bearing: the caller treats it as fail-open (self_contained).
    Only explicit true/false-like values decide; a junk string must NOT coerce to
    False and block a valid registration.
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


def _coerce_missing(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


async def check_action_self_contained(
    *,
    llm,
    action: str,
    request_context: str = "",
) -> ActionCheckResult:
    """Decide whether ``action`` can be executed later with no other context.

    Runs one structured tool-calling completion on ``llm`` (the low-performance
    model). Fail-open: any missing/unparseable decision yields
    ``self_contained=True``. Raising propagates to the caller, which also
    fail-opens.
    """
    tools = _build_check_tools()
    messages = [
        {"role": "system", "content": _CHECK_SYSTEM_PROMPT},
        {"role": "user", "content": _format_check_prompt(action, request_context)},
    ]

    function_calls: List[Dict[str, Any]] = []
    tokens: Dict[str, int] = done_chunk_token_usage({})

    # tool_choice="auto" mirrors the matching gate (works across all configured
    # providers); the lone tool + strong instruction make a call the default, and
    # the no-call branch fails open anyway.
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
            tokens = done_chunk_token_usage(response)
            break

    if not function_calls:
        logger.warning(
            "[action_check] model produced no decision tool call; failing open"
        )
        return ActionCheckResult(True, [], "no decision returned; defaulted to pass", tokens)

    call = function_calls[0]
    args = call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    self_contained = _coerce_bool(args.get("self_contained"))
    missing = _coerce_missing(args.get("missing"))
    reason = str(args.get("reason") or "").strip()

    if self_contained is None:
        logger.warning("[action_check] unparseable 'self_contained' value; failing open")
        return ActionCheckResult(True, [], reason or "ambiguous decision; defaulted to pass", tokens)

    return ActionCheckResult(
        self_contained,
        missing if not self_contained else [],
        reason or ("self-contained" if self_contained else "references missing info"),
        tokens,
    )


def build_rejection_message(*, tool_name: str, missing: List[str], reason: str) -> str:
    """The observation returned to the model when a registration is rejected."""
    detail = "; ".join(missing).strip() or (reason.strip() or "a referenced value")
    return (
        "Registration rejected: the action is not self-contained. When it fires "
        "it runs in a FRESH conversation with no access to this one, so "
        "references like 'the provided URL' or 'the file mentioned above' cannot "
        "be resolved.\n"
        f"Missing from the action: {detail}.\n"
        f"Re-call {tool_name} with the SAME arguments, but rewrite `action` to "
        "inline every concrete value verbatim (full URLs, email addresses, file "
        "paths, IDs, search criteria). If you do not have a concrete value, ask "
        "the user for it instead of retrying."
    )


async def record_action_check_usage(
    *,
    llm: Any,
    tokens: dict,
    profile: str,
    tool_name: str,
    conversation_id: Optional[str],
) -> None:
    """Persist the check's LLM call as an ``event_gate`` usage record.

    Best-effort: accounting must never break a registration. Reuses the
    ``event_gate`` source kind (the existing UI chip) with a distinct label so
    the cost is visible on the registering conversation. Mirrors
    :func:`app.events.runner._record_gate_usage`.
    """
    if llm is None or not tokens or not any(tokens.values()):
        return
    try:
        from app.agent.usage import UsageRecord
        from app.storage import get_usage_storage

        record = UsageRecord(
            source_kind="event_gate",
            tool_id=None,
            label=f"Action check: {tool_name}",
            provider=getattr(llm, "provider_name", None),
            model=getattr(llm, "model_name", None),
            model_group=None,
            step_index=0,
            input_tokens=int(tokens.get("input_tokens") or 0),
            cache_read_input_tokens=int(tokens.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(tokens.get("cache_creation_input_tokens") or 0),
            output_tokens=int(tokens.get("output_tokens") or 0),
        )
        await get_usage_storage().add_usage_records(
            conversation_id=conversation_id,
            profile=profile,
            records=[record.to_dict()],
            message_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[action_check] failed to record usage")


async def gate_registration_action(
    *,
    profile: str,
    action: str,
    request_context: str = "",
    tool_name: str,
    conversation_id: Optional[str] = None,
) -> Optional[ActionCheckResult]:
    """Run the self-containment gate before persisting a registration.

    Returns the failing :class:`ActionCheckResult` when the action must be
    rewritten; returns ``None`` when the action passes OR when the gate
    fail-opens (no LLM available, timeout, or any error). Callers reject only on
    a non-``None`` result.
    """
    action = (action or "").strip()
    if not action:
        # An empty action is caught by each tool's own validation; nothing to judge.
        return None

    try:
        from app.events.runner import get_cremind_agent

        agent = get_cremind_agent()
        if agent is None:
            return None  # server globals not wired (tests / slim CLI) → fail open
        llm = agent.low_performance_llm(profile)
    except Exception:  # noqa: BLE001
        logger.exception("[action_check] LLM unavailable; failing open")
        return None

    try:
        result = await asyncio.wait_for(
            check_action_self_contained(
                llm=llm, action=action, request_context=request_context,
            ),
            timeout=_CHECK_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — timeout or provider error
        logger.exception("[action_check] check failed; failing open")
        return None

    await record_action_check_usage(
        llm=llm, tokens=result.tokens, profile=profile,
        tool_name=tool_name, conversation_id=conversation_id,
    )

    if result.self_contained:
        return None
    logger.info(
        f"[action_check] rejected {tool_name} registration: {result.reason!r} "
        f"missing={result.missing}"
    )
    return result
