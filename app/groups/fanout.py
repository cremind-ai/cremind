"""Posting to a group: one timeline row, then a turn in every other member's seat.

This is the whole feature in one function. :func:`post_message` records what was
said, tells every open client, and hands it to each of the other members.
Everything else in ``app/groups`` either feeds it (the API, the tool) or is
called by it.

Delivery to a member is deliberately the same shape as an inbound channel message
(:meth:`app.channels.base.BaseChannelAdapter._dispatch_to_agent`): try to fold it
into a turn already running, otherwise start one. A group is a place where
several agents are busy at once, so "somebody said something while you were
mid-sentence" is the normal case here rather than the exception — and folding it
in is what lets one reply cover everything that arrived, instead of the agent
answering a stale question and then answering again.

**The hop counter** is what keeps a room of agents from talking forever. A human
message is hop 0; an agent answering it posts hop 1; an agent answering *that*
posts hop 2. At the group's ``max_agent_hops`` delivery continues but no turns
are started: the agents still SEE the conversation (so their history is
coherent) and simply stop replying until a human speaks again, which resets the
count. It is derived from the timeline rather than tracked in memory, so a turn
that dies and is re-run cannot quietly restart the chain.

**Routing** (:mod:`app.groups.routing`) trims the same fan-out from the other
end: one cheap-model call names the members worth starting a turn for, and the
rest are delivered the quiet way. It decides who STARTS, never who may speak —
a woken agent still answers or goes ``[silent]`` on its own judgement, and a
router that fails, times out or is unsure wakes everybody, which is the
behaviour of a room without it. So a message reaches every seat either way: the
only thing routing can take away is a turn, never a member's history. A capped
post is not classified at all: nothing may start a turn there whatever the
answer would have been, so the call would buy a discarded decision at the price
of a model round trip — during the exact failure the caps exist to contain.

**Order is decided by arrival, never by the classifier.** Inbound is
concurrent on purpose — the channel adapters start a task per update — so
several posts are routinely in this function at once, and each of them is one
provider round trip long. Everything that positions a post therefore happens
under the group lock and nothing else does: ``ordering`` and the hop count are
both derived from the rows already in the timeline, so the row is written first
and classified afterwards. The order this protects is not academic. With the
classification in front of the insert, "book us a table" (4s to classify) and
"actually make it 8pm" (0.4s, sent a second later) reached the timeline
inverted, and every seat then read the correction before the thing it
corrected. Delivery runs outside the lock — a member's seat can take a while,
and no other post should wait for that — but it still goes in timeline order,
because ``_delivery_chain`` makes each post wait for the one before it to
finish handing itself out.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.groups import settings as group_settings
from app.groups.render import (
    ROUTING_NOTE_EVERYONE,
    render_attributed,
    routing_note_for_names,
)
from app.groups.shadow import ensure_shadow_conversation
from app.utils.logger import logger

if TYPE_CHECKING:  # the module itself is imported late, to keep the LLM stack out
    from app.groups.routing import RoutingDecision

# One lock per group: ``ordering`` and the hop count are both derived from the
# rows already in the timeline, so an interleaved insert would corrupt both.
_group_locks: Dict[str, asyncio.Lock] = {}

# One future per group, resolved when that group's newest post has finished
# delivering. Each post takes the current tail while it still holds the lock and
# waits on it before touching a seat, so the fan-out runs in timeline order even
# though it no longer runs under the lock. Without it the order would be decided
# by whichever classification finished first, which is the reordering the insert
# was moved ahead of the routing call to prevent. The entry is dropped by the
# last post to use it, so an idle group holds nothing.
_delivery_chain: Dict[str, "asyncio.Future[None]"] = {}

# How much of a seat's history to hand a new turn when compaction is off or
# fails. Compaction normally rebuilds this from the DB itself; this fallback
# takes the NEWEST messages, unlike ``get_messages()`` which takes the oldest.
_FALLBACK_HISTORY_MESSAGES = 60

# How much of the room the router gets to see, the post being classified
# included (it is already in the timeline by then, and the router drops it from
# the rendered history). Enough to recognise "yes, go ahead" as the
# continuation of an exchange with one agent; short enough that the
# classification stays the cheap half of the decision.
_ROUTING_HISTORY_ROWS = 8

# How long a post waits for the one ahead of it to finish delivering before it
# goes anyway. Ordering is worth waiting for — that is the chain's whole job —
# but not unboundedly: a seat's turn is not finished until its post has been
# handed out, so a single stuck delivery would otherwise hold every member's
# turn-completion and leave the room's "thinking" indicators lit forever. Far
# above any real delivery (two classifications and a few seat writes), so
# reaching it means something is wrong rather than slow.
_DELIVERY_CHAIN_TIMEOUT_S = 60.0


def _lock_for(group_id: str) -> asyncio.Lock:
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = asyncio.Lock()
        _group_locks[group_id] = lock
    return lock


def _storages():
    from app.events import runner as event_runner
    from app.storage import get_group_chat_storage

    return get_group_chat_storage(), event_runner.get_conversation_storage()


async def post_message(
    *,
    group_id: str,
    sender_kind: str,
    sender_name: str,
    content: str,
    sender_profile: Optional[str] = None,
    sender_identity: Optional[Dict[str, Any]] = None,
    hop: Optional[int] = None,
    source_conversation_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    segment: int = 0,
    originated_from_shadow_turn: bool = False,
    deliver_only: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Post one message to a group and fan it out.

    Returns the timeline row, or ``None`` when nothing was posted — an unknown
    group, empty text, or a duplicate (a re-post of an agent turn). ``None`` is a
    normal outcome, not an error: the caller simply does nothing further.

    ``hop`` is computed when omitted. ``deliver_only`` forces the no-turn path
    regardless of hop — used for system notices, which everyone should see and
    nobody should answer.
    """
    text = (content or "").strip()
    if not group_id or not text:
        return None

    storage, conversation_storage = _storages()
    if storage is None or conversation_storage is None:
        logger.error("[group] storage not initialized; cannot post")
        return None

    group = await storage.get_group(group_id)
    if group is None:
        logger.warning(f"[group] post to unknown group {group_id}")
        return None

    members: List[str] = list(group.get("members") or [])
    if sender_kind == "agent" and sender_profile and sender_profile not in members:
        raise ValueError("not_a_member")

    settings = group_settings.normalize_settings(group.get("settings"))

    # Our place in this group's delivery order, claimed under the lock below.
    mine: Optional["asyncio.Future[None]"] = None
    try:
        async with _lock_for(group_id):
            resolved_hop = await _resolve_hop(
                storage,
                group_id=group_id,
                sender_kind=sender_kind,
                sender_profile=sender_profile,
                hop=hop,
            )
            flooded = await _is_flooding(storage, group_id, sender_kind, settings)
            capped = (
                deliver_only
                or flooded
                or (
                    sender_kind == "agent"
                    and resolved_hop >= group_settings.max_agent_hops(settings)
                )
            )

            row_metadata = dict(metadata or {})
            if capped:
                row_metadata["quiet"] = True
                row_metadata["quiet_reason"] = (
                    "system" if deliver_only else ("flood" if flooded else "hop_limit")
                )

            row = await storage.add_message(
                group_id=group_id,
                sender_kind=sender_kind,
                sender_name=sender_name,
                content=text,
                hop=resolved_hop,
                sender_profile=sender_profile,
                sender_identity=sender_identity,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                segment=segment,
                delivered_to=[sender_profile] if sender_profile else [],
                metadata=row_metadata or None,
            )
            if row is None:
                logger.debug(
                    f"[group] duplicate post ignored in {group_id} "
                    f"(source_message_id={source_message_id!r}, segment={segment})"
                )
                return None

            await storage.touch_group(group_id)
            # Published from inside the lock so the room's live view is in the
            # same order as the timeline it is a view of, and published before
            # the classification so a post appears the moment it is recorded
            # rather than a provider round trip later.
            await _publish(group_id, "message", row)

            if capped:
                logger.info(
                    f"[group] {group_id}: message #{row['ordering']} delivered "
                    f"without starting turns "
                    f"({row_metadata.get('quiet_reason')}, hop={resolved_hop})"
                )

            previous = _delivery_chain.get(group_id)
            mine = asyncio.get_running_loop().create_future()
            _delivery_chain[group_id] = mine

        # Nothing may start a turn on a capped post, so there is nothing for a
        # classification to narrow: skipping it saves a model call and up to ten
        # seconds of latency exactly when the room is looping or flooding, and
        # leaves the row unstamped — a routing stamp on a post that woke nobody
        # reads, in the UI, as "these agents answered" on the very row where the
        # question is why nobody did.
        decision: Optional["RoutingDecision"] = None
        if not capped:
            decision = await _route(
                storage=storage,
                group=group,
                settings=settings,
                row=row,
                # Only a seat's own turn coming back may be routed to nobody.
                # The row cannot tell that apart from a ``send_group_message``
                # or ``as_profile`` post — all three are ``agent`` — and those
                # two are somebody deliberately addressing the room.
                nobody_eligible=originated_from_shadow_turn,
            )
            if decision is not None:
                await _stamp_routing(storage, row, decision)

        if previous is not None:
            # The post before this one is still handing itself to the seats.
            # Bounded, because everything behind it waits: a seat's turn does
            # not finish until its own post has been fanned out, so one wedged
            # delivery would hold every other member's ``complete`` frame and
            # leave "X is thinking" lit indefinitely. Past the bound the room
            # would rather have this post out of order than not at all — and
            # the wait is long enough that only a hang reaches it, never a slow
            # classification.
            try:
                await asyncio.wait_for(previous, timeout=_DELIVERY_CHAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[group] {group_id}: the previous post is still delivering "
                    f"after {_DELIVERY_CHAIN_TIMEOUT_S:.0f}s; going ahead out of "
                    "order rather than holding the room"
                )

        rendered = render_attributed(sender_name, sender_kind, text)
        delivered: List[str] = []
        for member in members:
            if sender_profile and member == sender_profile:
                continue
            # The caps are absolute and are decided first: routing may only
            # take turns away, never hand one back to a room that is looping or
            # flooding. Below them, a member the router passed over takes the
            # same quiet path — delivered, not woken.
            routed_away = not capped and not _starts_turn(decision, member)
            try:
                await _deliver_to_member(
                    member=member,
                    group=group,
                    rendered=rendered,
                    row=row,
                    capped=capped or routed_away,
                    conversation_storage=conversation_storage,
                    routed_away=routed_away,
                )
                delivered.append(member)
                # Recorded as we go: a crash halfway through leaves an accurate
                # record of who already has it, and the boot sweep finishes the
                # rest rather than delivering to everybody twice.
                await storage.update_delivered_to(row["id"], [member])
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[group] failed to deliver message {row['id']} to {member}"
                )

        if sender_kind == "agent" and sender_profile and not originated_from_shadow_turn:
            await _materialise_own_post(
                conversation_storage=conversation_storage,
                group=group,
                profile=sender_profile,
                row=row,
                text=text,
            )

        row["delivered_to"] = list(dict.fromkeys(
            list(row.get("delivered_to") or []) + delivered
        ))

        return row
    finally:
        # Whatever happened — a raise, a cancelled task at shutdown — the post
        # waiting behind this one is released. A chain that could stall would
        # take the whole room's fan-out down with it.
        if mine is not None:
            if not mine.done():
                mine.set_result(None)
            if _delivery_chain.get(group_id) is mine:
                del _delivery_chain[group_id]


async def _resolve_hop(
    storage: Any,
    *,
    group_id: str,
    sender_kind: str,
    sender_profile: Optional[str],
    hop: Optional[int],
) -> int:
    if hop is not None:
        return max(0, int(hop))
    if sender_kind != "agent" or not sender_profile:
        # A person starts the conversation over.
        return 0
    # Measured from the last thing a PERSON said, not from this agent's own last
    # post. Measuring per agent looks equivalent and is not: after a chain runs
    # to the limit and a human then speaks, an agent's own last post still sits
    # below several higher-hop ones, so it would inherit them and stay capped —
    # the room would go quiet permanently and no amount of asking would revive it.
    floor = await storage.last_user_ordering(group_id)
    highest = await storage.max_agent_hop_after(group_id, floor)
    # Nothing since the last person spoke: this agent is answering them directly,
    # which is one step away — never zero, or an agent posting from a scheduled
    # run could restart the chain at will and talk to the room forever.
    return 1 if highest < 0 else highest + 1


async def _is_flooding(
    storage: Any, group_id: str, sender_kind: str, settings: Dict[str, Any],
) -> bool:
    if sender_kind != "agent":
        return False
    limit = group_settings.max_agent_posts_per_minute(settings)
    if limit <= 0:
        return False
    recent = await storage.count_agent_posts_since(
        group_id, time.time() * 1000 - 60_000,
    )
    return recent >= limit


def _starts_turn(decision: Optional["RoutingDecision"], member: str) -> bool:
    """Whether this post should start a turn in ``member``'s seat.

    ``None`` means routing did not run, and wakes everyone — which is what keeps
    the skipped cases (a system notice, a two-member room, the knob off) on
    exactly the behaviour the room had before routing existed.
    """
    if decision is None:
        return True

    from app.groups.routing import should_start_turn

    return should_start_turn(decision, member)


async def _route(
    *,
    storage: Any,
    group: Dict[str, Any],
    settings: Dict[str, Any],
    row: Dict[str, Any],
    nobody_eligible: bool = False,
) -> Optional["RoutingDecision"]:
    """Ask the classifier who to wake, or ``None`` when it does not apply.

    Skipped rather than answered "everyone" wherever a call could not change the
    outcome: a post nobody is meant to answer (the system notices — the caller
    skips every other capped post before it gets here), the knob off, and a room
    with too few possible answerers for the question to have a second answer,
    where the classification would cost more than the turn it saves.

    Nothing here may raise. The model is a hint about who speaks first; a room
    that stopped delivering messages because a provider was down would be a far
    worse failure than one that woke everybody.
    """
    from app.groups import routing

    sender_profile = row.get("sender_profile")
    if row.get("sender_kind") == "system":
        return None
    if not group_settings.routing_enabled(settings):
        return None
    nobody_eligible = bool(nobody_eligible) and row.get("sender_kind") == "agent"
    candidates = [m for m in (group.get("members") or []) if m != sender_profile]
    # The same rule ``route_message`` applies, checked here too so a room that
    # cannot benefit never resolves a model in the first place.
    if len(candidates) < routing.min_candidates(nobody_eligible):
        return None

    llm: Any = None
    try:
        from app.events.runner import get_cremind_agent

        # The room's creator pays for the classification and picks the model:
        # the decision is about the room, and no one member's seat owns it.
        llm = get_cremind_agent().low_performance_llm(
            profile=group.get("created_by") or "admin",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[group] could not resolve a routing model; waking every member"
        )

    try:
        # Read AFTER the insert, so the slice contains the post being classified
        # and everything the room said before it. Read before the insert — which
        # is what running in front of the lock forced — a burst of messages was
        # each classified against a history missing the ones just ahead of it,
        # and a router handed half a conversation does not hesitate: it returns
        # a confident, narrow answer that then gates the fan-out.
        # ``route_message`` drops this row from the rendered history by id.
        recent = await storage.list_messages(
            group["id"], limit=_ROUTING_HISTORY_ROWS, newest_first=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[group] could not read the room for routing")
        recent = []

    try:
        decision = await routing.route_message(
            group=group,
            settings=settings,
            row=row,
            recent_rows=recent,
            llm=llm,
            nobody_eligible=nobody_eligible,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[group] routing raised; waking every member")
        return routing.RoutingDecision(
            reason="routing raised; defaulted to everyone", errored=True,
        )

    await _record_routing_usage(decision, llm, group)
    return decision


async def _stamp_routing(
    storage: Any, row: Dict[str, Any], decision: "RoutingDecision",
) -> None:
    """Record who the router woke, on the row itself.

    On the row rather than in memory so the room can show who was woken and why,
    and so the boot sweep can finish an interrupted fan-out without
    re-classifying — a second call could answer differently, and would start
    turns this one declined.

    A second write rather than part of the insert because the classification
    only exists once the row does (see the module docstring). Its failure is
    logged and swallowed: an unstamped row loses the chip and makes the sweep
    fall open to waking everyone, which is the outcome routing already defaults
    to, whereas raising here would cost the room the message itself.

    And a second FRAME, for the same reason: the row went out to the room from
    inside the lock, which is what lets a post appear the moment it is recorded
    instead of a provider round trip later — but that is necessarily before this
    decision exists, so the message frame already on the wire carries no stamp.
    Without this a live viewer never sees the chip and only a reload explains why
    half the room stayed quiet, which is the one moment the chip is for. Sent
    from here rather than from the caller so the stamp and its broadcast cannot
    drift apart.
    """
    stamp = {
        "targets": sorted(decision.targets),
        "everyone": bool(decision.everyone),
        "nobody": bool(decision.nobody),
        "reason": decision.reason,
        "errored": bool(decision.errored),
        "model": decision.model,
    }
    row["metadata"] = {**(row.get("metadata") or {}), "routing": stamp}
    try:
        await storage.update_message_metadata(row["id"], {"routing": stamp})
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] could not record the routing decision on {row['id']}"
        )
    # Ephemeral: a client that reconnects is caught up from the replay ring, and
    # the ring holds this very ``row`` object, whose metadata the line above just
    # rebound — so the replayed message frame already carries the stamp and a
    # ringed copy of it would only cost the room's 200-frame window, halving how
    # far back a reconnecting client can catch up on actual messages.
    await _publish(
        str(row.get("group_id") or ""), "message_routing",
        {"message_id": row["id"], "routing": stamp},
        ephemeral=True,
    )


async def _record_routing_usage(
    decision: "RoutingDecision", llm: Any, group: Dict[str, Any],
) -> None:
    """Bill the classification to the room's owner. Best-effort.

    Recorded against no conversation, the shape
    :func:`app.events.runner._record_gate_usage` uses for a call that precedes
    every conversation: this one belongs to the room, and charging it to
    whichever member happened to be woken would misattribute it.
    """
    try:
        from app.groups.routing import routing_usage_record
        from app.storage import get_usage_storage

        record = routing_usage_record(
            decision, llm, group_name=str(group.get("name") or ""),
        )
        if record is None:
            return
        await get_usage_storage().add_usage_records(
            conversation_id=None,
            profile=group.get("created_by") or "admin",
            records=[record.to_dict()],
            message_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[group] failed to record the routing usage")


def _routing_note(
    row: Dict[str, Any], member: str, *, capped: bool, routed_away: bool,
) -> str:
    """The line telling ``member`` who this post was routed to, or "".

    The router already knows something the agent spends a whole turn guessing at
    — whether the message was for it — and until now it threw that away: a
    member woken by "hello everyone" read the same bare line as one who happened
    to be copied in, and decided for itself, wrongly, that a greeting to the room
    was not its business.

    Derived from the routing stamp and the two delivery flags rather than from
    ``capped`` alone, because callers fold ``routed_away`` into that argument:
    the passed-over copy is precisely the one that should say who *was* asked, so
    reading it as "capped" would drop the note exactly where it explains most.

    Silent (and deliberately so) when nothing was decided: no stamp, a router
    that errored, a ``nobody`` outcome, or a row capped in its own right. A note
    is a statement about who is expected to answer, and on those rows the honest
    answer is nothing at all.
    """
    if bool((row.get("metadata") or {}).get("quiet")):
        return ""

    from app.groups.routing import decision_from_stamp
    from app.utils.agent_name import read_agent_name

    # The same reader the boot sweep uses, so a stamp cannot mean one thing to
    # the note and another to the decision about who was woken.
    decision = decision_from_stamp((row.get("metadata") or {}).get("routing"))
    if decision is None or decision.errored or decision.nobody:
        return ""

    targets = sorted(decision.targets)
    if routed_away:
        # Delivered without a turn: name whoever the router did wake, so a later
        # turn reading its history can see why this one was not its to answer.
        return routing_note_for_names([read_agent_name(t) for t in targets])
    if capped:
        return ""
    if decision.everyone:
        return ROUTING_NOTE_EVERYONE
    if member in targets:
        others = [read_agent_name(t) for t in targets if t != member]
        return routing_note_for_names(["you", *others])
    return ""


async def _deliver_to_member(
    *,
    member: str,
    group: Dict[str, Any],
    rendered: str,
    row: Dict[str, Any],
    capped: bool,
    conversation_storage: Any,
    routed_away: bool = False,
) -> None:
    """Put one message into one member's seat, starting a turn unless capped."""
    conv = await ensure_shadow_conversation(conversation_storage, member, group)
    if conv is None:
        return
    conversation_id = conv["id"]

    # Baked into the text, like the sender prefix and for the same reason: the
    # model is handed ``role`` and ``content`` and nothing else, so an
    # annotation recorded anywhere but the content is one the agent never reads.
    # Applied once, here, before every sink below — the quiet write, the parked
    # copy and the enqueued turn must all persist the identical string, or a
    # seat's history stops being a verbatim prefix of itself and the prompt
    # cache is lost on every turn.
    note = _routing_note(row, member, capped=capped, routed_away=routed_away)
    if note:
        rendered = f"{rendered}\n{note}"

    metadata = {
        "source": "group_chat",
        "group": {
            "group_id": group.get("id"),
            "group_name": group.get("name"),
            "message_id": row.get("id"),
            "ordering": row.get("ordering"),
            "sender_kind": row.get("sender_kind"),
            "sender_profile": row.get("sender_profile"),
            "sender_name": row.get("sender_name"),
            "hop": row.get("hop"),
            "quiet": bool(capped),
            # Tells the two quiet reasons apart for whoever reads the seat
            # later: "the room is looping" and "the router thought this one was
            # not for you" look identical on the row without it.
            "routed_away": bool(routed_away),
        },
    }

    if capped:
        # Visible to the agent on its next real turn, but it starts none. Written
        # straight to the row rather than parked: parking would hand it to the
        # turn-end flush, which would start exactly the turn the cap exists to
        # prevent.
        await conversation_storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=rendered,
            metadata=metadata,
        )
        return

    from app.agent.stream_runner import make_run_id
    from app.events import queue as event_queue
    from app.events import user_message_delivery

    parked = await user_message_delivery.try_park_user_message(
        conversation_id=conversation_id,
        profile=member,
        query=rendered,
        user_message_metadata=metadata,
    )
    if parked is not None and parked.injected:
        # Folded into the turn already running — that turn's answer covers it.
        return

    history = await _fallback_history(conversation_storage, conversation_id, member)
    await event_queue.enqueue_user_message(
        conversation_id=conversation_id,
        run_id=make_run_id(conversation_id, kind="group"),
        profile=member,
        query=rendered,
        history_messages=history,
        reasoning=True,
        user_message_metadata=metadata,
        # A message that was parked and then lost the race to the turn's end is
        # already persisted; run it without persisting it twice.
        push_user_message=parked is None,
        existing_user_message_id=parked.message_id if parked is not None else None,
        update_title_from_query=False,
    )


async def _materialise_own_post(
    *,
    conversation_storage: Any,
    group: Dict[str, Any],
    profile: str,
    row: Dict[str, Any],
    text: str,
) -> None:
    """Record a post the agent made from OUTSIDE its seat in its seat's history.

    When an agent posts via the ``send_group_message`` tool, the words never
    passed through its group conversation — so without this the agent would
    later read replies to something it has no memory of saying.
    """
    conv = await ensure_shadow_conversation(conversation_storage, profile, group)
    if conv is None:
        return
    try:
        await conversation_storage.add_message(
            conversation_id=conv["id"],
            role="agent",
            content=text,
            metadata={
                "group": {
                    "group_id": group.get("id"),
                    "kind": "materialised",
                    "posted_message_ids": [row.get("id")],
                }
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] failed to mirror {profile}'s own post into its seat"
        )


async def _fallback_history(
    conversation_storage: Any, conversation_id: str, profile: str,
) -> List[Any]:
    """History for a new turn, newest-first-trimmed.

    Only used when compaction is disabled or errors (it rebuilds the history from
    the database itself). Deliberately not ``get_messages()``, which returns the
    OLDEST rows and would hand a busy seat a history that stops before the
    message being answered.
    """
    try:
        from app.config.user_config import replay_reasoning_enabled
        from app.utils.common import convert_db_messages_to_history

        rows = await conversation_storage.get_messages_after(
            conversation_id, -1,
            limit=_FALLBACK_HISTORY_MESSAGES, newest_first=True,
        )
        if not rows:
            return []
        return convert_db_messages_to_history(
            rows, include_reasoning=replay_reasoning_enabled(profile),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] failed to load fallback history for {conversation_id}"
        )
        return []


async def _publish(
    group_id: str, event_type: str, data: Any, *, ephemeral: bool = False,
) -> None:
    try:
        from app.groups.bus import get_group_stream_bus

        await get_group_stream_bus().publish(
            group_id, event_type, data, ephemeral=ephemeral,
        )
    except Exception:  # noqa: BLE001
        logger.debug(f"[group] failed to publish {event_type}", exc_info=True)
