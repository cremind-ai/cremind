"""Which agents should even start a turn — a cheap hint, never a veto.

Every post in a group is delivered to every other member's seat, and each seat
runs a FULL reasoning turn just to work out that the message was not for it and
answer ``[silent]``. In a room of five agents one "@rex what's on my calendar?"
costs five system prompts, five tool catalogues and five histories to produce one
answer. This module spends one call on the cheap ``low`` model first and names
the agents worth waking.

**It narrows who starts, and nothing else.** The per-agent ``[silent]``
self-determination in :mod:`app.groups.hooks` is untouched: a chosen agent still
decides for itself whether it has anything to say. That division is deliberate —
routing is allowed to be wrong in one direction only. An agent it wrongly
includes costs one turn and declines by itself; an agent it wrongly *excludes*
cannot answer at all and the room simply never hears from it, with nothing in the
transcript to explain the silence. So every uncertain path here resolves to
``everyone``:

- routing switched off, no model wired, an exception, a timeout;
- no tool call, arguments that will not parse, a decision that names only
  profiles this group has never heard of;
- an empty target set, for any reason at all.

The sender is always dropped (it does not answer itself), which is why the roster
handed to the model is built from the other members alone.

**One post can legitimately be for nobody, and only one kind.** When an agent
finishes a turn its answer is posted back to the room, and that post is fanned
out like any other — so "Cremind: I'm doing well, how can I help?" starts a turn
in every other seat, each of which spends a full reasoning turn to conclude the
remark was not for it and answer ``[silent]``. Nothing above can express that:
"unsure" and "nobody" are different answers, and only the second is right for a
reply that already went to the person who asked. ``nobody`` is therefore honoured
for exactly one shape — a member's own turn coming back through
:func:`app.groups.hooks.on_shadow_turn_complete` — and never for a person's
message, a ``send_group_message`` tool post or an ``as_profile`` post, all of
which keep the old rule that somebody must be woken. It is a *confident*
narrowing, not an uncertain one; every uncertain path still resolves to
``everyone``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from app.constants import ChatCompletionTypeEnum
from app.groups.constants import ROUTING_SETTING_KEY as _ROUTING_SETTING_KEY
from app.groups.render import render_attributed
from app.lib.llm.base import done_chunk_token_usage
from app.utils.agent_name import read_agent_name
from app.utils.logger import logger
from app.utils.persona import read_persona_file

_TOOL_NAME = "route"

# The group-settings flag, re-exported so this module's callers need not know
# where it lives. Spelled ONCE, in ``constants``: it was briefly declared here
# too, with a comment claiming an absent key reads as OFF — the opposite of
# what ``settings.routing_enabled`` and ``DEFAULT_ROUTING_ENABLED`` actually do,
# and the same drift the ``routing_enabled`` docstring below describes being
# fixed once already.
ROUTING_SETTING_KEY = _ROUTING_SETTING_KEY

# Upper bound on the whole classification. A hung provider must not hold up
# delivery to the room: at that point the honest answer is "wake everyone", which
# is what the caller does anyway when this fires.
_ROUTING_TIMEOUT_S = 10.0

# Tighter bound for a seat's own reply. That classification runs inside the
# turn's finalization: the room already has the text (it is published under the
# lock, before this), but the seat's ``complete`` frame, its idle status and the
# mid-turn inbox flush all wait for the fan-out behind it — so a slow provider
# leaves "X is thinking" lit for seconds after X's answer is on screen. A
# person's post has no such tail and keeps the full budget.
#
# It bounds this post's own classification and nothing else: a post still queues
# behind the delivery of the one before it (``fanout._delivery_chain``), which
# may be a person's message spending the full budget above.
_ROUTING_TIMEOUT_AGENT_S = 4.0

# The roster's job is to tell one agent apart from another, not to reproduce a
# persona. The opening lines carry the role; the rest is tone and house rules
# that make every profile look alike to a classifier.
_PERSONA_MAX_CHARS = 200

_RECENT_ROWS = 6
_RECENT_TEXT_MAX_CHARS = 300
_NEW_MESSAGE_MAX_CHARS = 2000

_ROUTING_SYSTEM_PROMPT = (
    "You are the router for a group chat. Several AI assistants sit in the room, "
    "each one belonging to a different person and each with its own role. You are "
    "given the ROSTER of assistants, the RECENT messages, and the NEW message "
    "that just arrived. Decide which assistants should reply to the NEW message.\n\n"
    "Rules:\n"
    "- Addressed by name → just those assistants.\n"
    "- Clearly matching one assistant's stated role and no other's → just that "
    "one.\n"
    "- Continuing an exchange the recent messages show is with one assistant → "
    "that assistant.\n"
    "- A general question, a greeting, or anything aimed at the room ('everyone', "
    "'all of you', 'who can…') → everyone=true.\n"
    "- The NEW message may itself be an assistant's. An assistant's post that "
    "only answers or reports back to a person, asking nothing of any other "
    "assistant on the ROSTER, → nobody=true: no assistant should reply. Rex "
    "answering Alexa's question, or reporting that a task is done, is the case "
    "this is for. If it asks another assistant for something, or leaves a "
    "question open for the room, name that assistant instead (or everyone=true).\n"
    "- nobody is only ever right for an assistant's post. A person's message "
    "always deserves at least one assistant: never return nobody for one.\n"
    "- When you are unsure, or more than one assistant could reasonably be meant, "
    "return everyone=true. Choosing too few is the only harmful mistake: an "
    "assistant you leave out never sees its turn and cannot answer, while an "
    "extra one costs a moment and stays quiet on its own.\n"
    "- Put the exact profile id from the ROSTER in `targets`. Never invent an id, "
    "and never name an assistant that is not on the ROSTER.\n"
    f"Report your decision by calling the {_TOOL_NAME} function."
)


@dataclass
class RoutingDecision:
    """Who should start a turn for one post.

    Defaults are the fail-open answer, so ``RoutingDecision()`` already means
    "wake everyone" and every early return can lean on it.

    ``errored`` marks a classification that could not be *performed* — no model,
    an exception, a timeout. A model that answered unusably (no tool call,
    unparseable arguments) is not an error: it decided nothing, and nothing is a
    valid outcome that resolves to everyone.

    ``nobody`` is the one answer the defaults cannot express, so it must be set
    together with ``everyone=False``: a decision built as
    ``RoutingDecision(nobody=True)`` alone would carry the default ``everyone=True``
    and stamp a self-contradiction onto the row, which every consumer would then
    have to know to read in the right order.
    """

    targets: Set[str] = field(default_factory=set)
    everyone: bool = True
    reason: str = ""
    tokens: Dict[str, int] = field(default_factory=lambda: done_chunk_token_usage({}))
    errored: bool = False
    model: Optional[str] = None
    nobody: bool = False


def routing_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    """Whether this group asked for pre-classification.

    Re-exported from :mod:`app.groups.settings` rather than reimplemented. It
    was briefly both, with opposite readings of an absent key — off here, on
    there — which no call path could expose (``post_message`` normalizes the
    blob first, so the key is always present by the time either ran) and which
    would therefore have gone on being wrong until the day something read a raw
    blob and silently took the other branch.
    """
    from app.groups.settings import routing_enabled as _resolve

    return _resolve(settings)


def should_start_turn(decision: RoutingDecision, profile: str) -> bool:
    """Whether ``profile`` is woken by this decision.

    The single place the hint is applied, so "narrows who starts" cannot drift
    into "decides who may speak" at a second call site.

    ``nobody`` is checked first and alone: it is the only field that can make
    this false for every member, and reading it after ``everyone`` would let a
    malformed stamp (both set) wake the room it was meant to leave alone.
    """
    if decision.nobody:
        return False
    return decision.everyone or profile in decision.targets


def _build_routing_tools() -> List[Dict[str, Any]]:
    """OpenAI-style single-function schema for the structured decision."""
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": (
                    "Report which assistants in the room should reply to the new "
                    "message."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The profile ids, copied exactly from the ROSTER, "
                                "of the assistants that should reply. Empty when "
                                "everyone is true."
                            ),
                        },
                        "everyone": {
                            "type": "boolean",
                            "description": (
                                "true when the message is for the whole room, or "
                                "whenever you are unsure which assistants are "
                                "meant."
                            ),
                        },
                        "nobody": {
                            "type": "boolean",
                            "description": (
                                "true when the new message is an assistant's own "
                                "post that asks nothing of any other assistant — "
                                "a reply to the person, a report, a closing "
                                "remark. Never true for a person's message."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "One short sentence explaining the choice, citing "
                                "the name or role you matched."
                            ),
                        },
                    },
                    "required": ["targets", "everyone", "nobody", "reason"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _persona_summary(profile: str) -> str:
    """The opening of a profile's persona, headings dropped, as one line.

    Headings go because they are the one part of a persona that is boilerplate
    across profiles ("## Role", "## Style"): keeping them would spend the budget
    on words that distinguish nobody.
    """
    try:
        text = read_persona_file(profile) or ""
    except Exception:  # noqa: BLE001 — a missing/unreadable persona is not fatal
        logger.debug(f"[group_routing] no persona for {profile}", exc_info=True)
        return ""
    parts: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts.append(stripped)
        if sum(len(p) for p in parts) > _PERSONA_MAX_CHARS:
            break
    joined = " ".join(parts)
    if len(joined) > _PERSONA_MAX_CHARS:
        joined = joined[:_PERSONA_MAX_CHARS].rstrip() + "…"
    return joined


def _build_roster(candidates: Sequence[str]) -> List[Dict[str, Any]]:
    """One entry per member that could be woken, sender already excluded."""
    roster: List[Dict[str, Any]] = []
    for profile in candidates:
        roster.append({
            "profile": profile,
            "name": read_agent_name(profile),
            "role": _persona_summary(profile),
        })
    return roster


def _format_roster(roster: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for entry in roster:
        role = entry.get("role") or "(no stated role)"
        lines.append(
            f"- profile_id={entry['profile']} | name={entry.get('name') or entry['profile']} "
            f"| role: {role}"
        )
    return "\n".join(lines)


def _format_row(row: Dict[str, Any], *, limit: int) -> str:
    text = str(row.get("content") or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return render_attributed(
        str(row.get("sender_name") or ""), str(row.get("sender_kind") or "user"), text,
    )


def _format_routing_prompt(
    *,
    group_name: str,
    roster: Sequence[Dict[str, Any]],
    row: Dict[str, Any],
    recent_rows: Sequence[Dict[str, Any]],
) -> str:
    # The row being routed is normally the newest one in the timeline slice the
    # caller read back, so drop it from the history rather than showing it twice
    # and inviting the model to route the copy.
    history = [
        r for r in (recent_rows or [])
        if isinstance(r, dict) and r.get("id") != row.get("id")
    ][-_RECENT_ROWS:]
    rendered_history = "\n".join(
        _format_row(r, limit=_RECENT_TEXT_MAX_CHARS) for r in history
    )
    return (
        f"ROOM: {group_name or 'group chat'}\n\n"
        f"ROSTER (the assistants that could reply):\n{_format_roster(roster)}\n\n"
        "RECENT MESSAGES (oldest first):\n"
        f"{rendered_history or '(none)'}\n\n"
        "NEW MESSAGE (decide who should reply to THIS one):\n"
        f"{_format_row(row, limit=_NEW_MESSAGE_MAX_CHARS)}\n\n"
        f"Call {_TOOL_NAME} with your decision."
    )


def _coerce_bool(value: Any) -> Optional[bool]:
    """Coerce an ``everyone`` value to a bool, or ``None`` when ambiguous.

    ``None`` matters: an unrecognised value must not read as False and quietly
    narrow the room on the strength of a junk string. It leaves the decision to
    ``targets``, which fails open when it comes out empty.
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
    return None


def _coerce_target_list(value: Any) -> List[str]:
    """Read ``targets`` however the provider spelled it."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # Not JSON — a bare "dog, cat" is the other thing models write here.
            value = value.split(",")
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return []


def _alias_map(roster: Sequence[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Everything a target might be spelled as → every profile it could mean.

    The prompt asks for profile ids and models hand back display names anyway;
    resolving them costs nothing and saves a whole fan-out.

    A spelling two members share maps to BOTH of them, and
    :func:`_resolve_targets` wakes both. That is the fail-open direction and the
    only one: an agent woken by mistake reads the message, sees it was not for
    it and answers ``[silent]`` at the cost of one turn, while an agent left out
    cannot answer at all and the room hears nothing to explain the silence. With
    members ``dog``/``cat``/``bird`` where cat calls its agent "Dog", neither
    picking the id nor dropping the key is right — one of them silences cat, the
    other silences dog. Waking both silences neither.
    """
    aliases: Dict[str, Set[str]] = {}

    def note(key: str, profile: str) -> None:
        key = key.strip().lower()
        if key:
            aliases.setdefault(key, set()).add(profile)

    for entry in roster:
        profile = entry["profile"]
        note(profile, profile)
        note(str(entry.get("name") or ""), profile)
    return aliases


def _resolve_targets(raw: Any, roster: Sequence[Dict[str, Any]]) -> Set[str]:
    """Map the model's target list onto real member profiles.

    One spelling can name more than one member — a profile id is unique, but a
    display name is whatever its owner typed into its settings and may collide
    with another member's id or name. Every candidate for a spelling is woken,
    because the two failure directions do not cost the same: an extra agent
    declines itself in one turn, a missing one is a question nobody answers.

    Anything that does not resolve is dropped rather than guessed at. Dropping
    everything is safe by construction: the caller turns an empty set into
    ``everyone``.
    """
    aliases = _alias_map(roster)
    resolved: Set[str] = set()
    for target in _coerce_target_list(raw):
        resolved |= aliases.get(target.strip().lower(), set())
    return resolved


def _names_a_member(text: str, roster: Sequence[Dict[str, Any]]) -> bool:
    """Whether the post spells out a member's name or id.

    The last guard on the one answer that fails CLOSED. Everywhere else an
    uncertain classification wakes everybody and costs a turn; ``nobody`` costs
    the room an answer nobody will ever ask for again, so "Mia, can you check
    the budget?" misread as a closing remark leaves Mia holding a question she
    was never woken to see.

    Deliberately cruder than the model it overrules: any roster spelling
    appearing in the text is enough to refuse, false positives included. Being
    wrong here costs one declined turn — the direction this module is built to
    fail in.
    """
    haystack = (text or "").lower()
    for spelling in _alias_map(roster):
        # Two-character ids would match inside ordinary words.
        if len(spelling) < 3:
            continue
        if re.search(rf"(?<!\w){re.escape(spelling)}(?!\w)", haystack):
            return True
    return False


async def _classify(
    *,
    llm: Any,
    group_name: str,
    roster: Sequence[Dict[str, Any]],
    row: Dict[str, Any],
    recent_rows: Sequence[Dict[str, Any]],
    nobody_eligible: bool = False,
) -> RoutingDecision:
    """One structured tool-calling completion, parsed into a decision.

    ``nobody_eligible`` decides whether a ``nobody`` answer is honoured at all;
    see :func:`route_message`. The schema always offers the field, because the
    tool definition is part of the cached prefix and must not vary per post — a
    stray ``nobody`` on anything else is simply ignored here.
    """
    messages = [
        {"role": "system", "content": _ROUTING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _format_routing_prompt(
                group_name=group_name, roster=roster, row=row, recent_rows=recent_rows,
            ),
        },
    ]

    function_calls: List[Dict[str, Any]] = []
    tokens: Dict[str, int] = done_chunk_token_usage({})
    model = getattr(llm, "model_name", None)

    # tool_choice="auto" mirrors the skill-event gate (it is what works across
    # every configured provider); the lone tool plus the closing instruction make
    # a call the overwhelming default, and the no-call branch fails open anyway.
    async for response in llm.chat_completion(
        messages=messages,
        tools=_build_routing_tools(),
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
            "[group_routing] model produced no routing tool call; waking everyone"
        )
        return RoutingDecision(
            reason="no routing decision returned; defaulted to everyone",
            tokens=tokens,
            model=model,
        )

    args = function_calls[0].get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    reason = str(args.get("reason") or "").strip()
    if _coerce_bool(args.get("everyone")) is True:
        return RoutingDecision(
            reason=reason or "addressed to the whole room", tokens=tokens, model=model,
        )

    targets = _resolve_targets(args.get("targets"), roster)
    if not targets and nobody_eligible and _coerce_bool(args.get("nobody")) is True:
        # Only once the named targets have come out empty: a decision that says
        # both "nobody" and "Mia" is contradictory, and waking Mia is the
        # forgiving reading of it.
        if _names_a_member(str(row.get("content") or ""), roster):
            logger.info(
                "[group_routing] refused a nobody decision on a post that names "
                "a member; waking everyone"
            )
            return RoutingDecision(
                reason=reason or "the post names a member; defaulted to everyone",
                tokens=tokens,
                model=model,
            )
        return RoutingDecision(
            targets=set(),
            everyone=False,
            nobody=True,
            reason=reason or "an assistant's own post; no reply expected",
            tokens=tokens,
            model=model,
        )
    if not targets:
        logger.info(
            "[group_routing] decision named no known member; waking everyone "
            f"(raw targets={args.get('targets')!r})"
        )
        return RoutingDecision(
            reason=reason or "no known assistant named; defaulted to everyone",
            tokens=tokens,
            model=model,
        )

    return RoutingDecision(
        targets=targets,
        everyone=False,
        reason=reason or "addressed to specific assistants",
        tokens=tokens,
        model=model,
    )


def decision_from_stamp(stamp: Any) -> Optional[RoutingDecision]:
    """Read a persisted routing stamp back into a decision, or ``None``.

    ``None`` means "this stamp cannot be trusted", and every caller answers it
    the way the classifier answers uncertainty: wake everybody. That covers a
    row written before routing existed, a truncated write, and a blob edited by
    hand — all of which used to be read leniently in one place and strictly in
    another, so the same corrupt stamp made the boot sweep quiet-deliver to
    every member (losing the post its only answer) while the note beside it
    named the targets letter by letter, ``[to: c, a, t]``, having iterated a
    bare string.

    One reader, so "what does this stamp say" has exactly one answer.
    """
    if not isinstance(stamp, dict):
        return None
    targets = stamp.get("targets")
    if targets is None:
        targets = []
    if not isinstance(targets, (list, tuple, set)):
        # A bare string is the shape that iterates per character. Anything that
        # is not a sequence of ids is unreadable, not empty.
        return None
    return RoutingDecision(
        targets={str(t) for t in targets if str(t or "").strip()},
        everyone=bool(stamp.get("everyone", True)),
        # Absent on every row written before the outcome existed, and false is
        # what those rows meant: somebody was woken.
        nobody=bool(stamp.get("nobody", False)),
        reason=str(stamp.get("reason") or ""),
        errored=bool(stamp.get("errored", False)),
        model=stamp.get("model") if isinstance(stamp.get("model"), str) else None,
    )


def min_candidates(nobody_eligible: bool) -> int:
    """How many possible answerers make a classification worth its round trip.

    Two, normally: with one candidate the only answers are "wake it" and "wake
    it", so there is nothing to narrow. But when ``nobody`` is on the table the
    one-candidate case has a real second answer — "this reply needs no reply" —
    and that is precisely the room shape (two members, one talking to the
    person) where the wasted turn is most obvious.

    Shared with :mod:`app.groups.fanout`, which applies the same rule before it
    resolves a model, so the two cannot drift into a pointless LLM lookup.
    """
    return 1 if nobody_eligible else 2


async def route_message(
    *,
    group: Dict[str, Any],
    settings: Optional[Dict[str, Any]],
    row: Dict[str, Any],
    recent_rows: Sequence[Dict[str, Any]] = (),
    llm: Any = None,
    nobody_eligible: bool = False,
) -> RoutingDecision:
    """Pick the members worth starting a turn for, or say "everyone".

    ``group`` is the storage dict (``id``, ``name``, ``members``), ``row`` the
    timeline row just inserted, ``recent_rows`` the last few rows oldest-first.

    ``llm`` is injected rather than resolved here — the same shape as
    :func:`app.events.gate.classify_event_match`, which is what lets this run
    under test without a live model. The caller passes
    ``get_cremind_agent().low_performance_llm(profile=<group creator or "admin">)``.

    ``nobody_eligible`` is the caller's statement that this post is a member's
    own turn coming back to the room (see the module docstring). It is passed in
    rather than derived from ``row`` because the row cannot tell a seat's reply
    from a ``send_group_message`` post or an ``as_profile`` one: all three are
    ``sender_kind="agent"``, and only the first may be routed to nobody.

    Never returns an empty target set unless ``nobody`` is set with it: every
    path that cannot produce a confident narrowing returns ``everyone=True``.
    """
    members = [p for p in (group or {}).get("members") or [] if p]
    sender = (row or {}).get("sender_profile")
    # The roster is the members MINUS the sender, which is the whole of "always
    # drop the sender": an id that is not on the roster cannot be resolved.
    candidates = [p for p in members if p != sender]

    # A person's message must always find an answerer, whatever the model says.
    nobody_eligible = bool(nobody_eligible) and (row or {}).get("sender_kind") == "agent"

    if not routing_enabled(settings):
        return RoutingDecision(reason="routing disabled for this group")

    if len(candidates) < min_candidates(nobody_eligible):
        # Nothing a classification could change: no possible answerer at all,
        # or one whose only alternative outcome ("nobody") is not on the table.
        return RoutingDecision(reason="too few candidates; nothing to narrow")

    if llm is None:
        logger.warning("[group_routing] no routing model available; waking everyone")
        return RoutingDecision(reason="no routing model available", errored=True)

    roster = _build_roster(candidates)

    try:
        decision = await asyncio.wait_for(
            _classify(
                llm=llm,
                group_name=str((group or {}).get("name") or ""),
                roster=roster,
                row=row or {},
                recent_rows=recent_rows or (),
                nobody_eligible=nobody_eligible,
            ),
            timeout=(
                _ROUTING_TIMEOUT_AGENT_S if nobody_eligible else _ROUTING_TIMEOUT_S
            ),
        )
    except Exception:  # noqa: BLE001 — timeout or provider error
        logger.exception("[group_routing] classification failed; waking everyone")
        return RoutingDecision(
            reason="routing failed; defaulted to everyone",
            errored=True,
            model=getattr(llm, "model_name", None),
        )

    if decision.nobody:
        logger.info(
            f"[group_routing] {group.get('id')}: waking nobody "
            f"of {len(candidates)} ({decision.reason})"
        )
    elif not decision.everyone:
        logger.info(
            f"[group_routing] {group.get('id')}: waking {sorted(decision.targets)} "
            f"of {len(candidates)} ({decision.reason})"
        )
    return decision


def routing_usage_record(
    decision: RoutingDecision, llm: Any, *, group_name: str = "",
) -> Optional[Any]:
    """The :class:`~app.agent.usage.UsageRecord` for this classification.

    Returns ``None`` when there is nothing to bill (routing was skipped, or the
    call never reported usage) so the caller can persist unconditionally. The
    field set mirrors :func:`app.events.runner._record_gate_usage`; only the
    ``source_kind`` differs, so the dashboard can tell a routing call apart from
    an event-gate one.

    ``group_name`` is a keyword because the decision deliberately carries no
    group identity — it is a hint about one message, not a record of one room.
    """
    tokens = decision.tokens or {}
    if llm is None or not any(tokens.values()):
        return None
    try:
        from app.agent.usage import UsageRecord

        return UsageRecord(
            source_kind="group_routing",
            tool_id=None,
            label=f"Routing: {group_name}" if group_name else "Routing",
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
    except Exception:  # noqa: BLE001 — accounting must never break the fan-out
        logger.exception("[group_routing] failed to build the usage record")
        return None
