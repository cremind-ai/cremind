"""What happens when a member speaks: its answer becomes its post.

An agent in a group does not "send a message" — it simply answers, and that
answer is what the room sees. Normally that is at the end of its turn, from the
one place every turn passes through on its way out (``stream_runner``'s
finalize), after the agent's message is safely persisted and before the terminal
``complete`` frame.

**A long turn can also speak part-way through.** When somebody interrupts a busy
agent and it chooses to answer (see ``_GROUP_ACK_REQUEST`` in the reasoning
agent), waiting for the turn to end would hold that reply back for as long as the
work takes — which is exactly the delay the interruption was trying to avoid. So
each flow break posts the segment it just closed, and the turn's end posts only
what is left. The two paths share ``_post_new_segments`` and differ in one
argument: mid-turn, the still-open tail is not posted.

Posting before the turn's message exists means those rows have no owner yet, so
they are keyed by the run (``live_source_key``) and re-pointed at the real
message when it persists. That key is what makes the whole thing idempotent: the
UNIQUE constraint on ``(source_message_id, segment)`` refuses a second copy, so
neither a re-run of the completion hook nor the boot sweep can say anything
twice.

Four things this has to get right:

**Silence is a real outcome.** Every member answers every message, so most turns
in a busy group should produce nothing at all. The agent says so with the
``[silent]`` sentinel and the turn posts nothing — but the sentinel is judged per
SEGMENT, because a turn interrupted mid-flight speaks twice ("Got it, checking"
then ``[silent]``) and testing the concatenation would post the sentinel.

**Every turn leaves a mark.** ``metadata.group`` is stamped on the agent's
message whatever happens — posted, silent, skipped. That stamp is how the boot
sweep tells a turn that already had its say from one the process died in the
middle of, and it is why a crash cannot double-post.

**A failed turn is not a silent one.** An agent that errors mid-room would
otherwise just stop existing from everyone else's point of view, so the room is
told — as a system notice nobody is expected to answer.

The other half of this module is the reverse direction: while a seat turn runs,
its frames are mirrored onto the room's stream so the room can show what each
agent is *doing*, not just what it eventually said.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.groups.constants import EMPTY_FINALS
from app.groups.render import (
    is_silent_ignoring_notes,
    split_segments,
    strip_routing_notes,
)
from app.groups.shadow import group_id_from_context
from app.utils.logger import logger

# What of a seat's run the room is allowed to see. Deliberately not ``text``:
# the room renders whole messages posted at turn end, so per-token streaming
# would race the post and show every member's answer twice. Also absent are the
# frames that address a single client's own session — ``user_message``,
# ``flow_break`` and the plan-mode frames (``todos``, plan approval, the
# question form) — which mean nothing to a spectator and, in plan mode, invite
# an answer nobody in the room can give.
SEAT_EVENT_TYPES = frozenset({
    "thinking",
    "result",
    "terminal",
    "cwd",
    "token_usage",
    "compaction_auto_folded",
    "error",
    "complete",
})


def live_source_key(run_id: str) -> str:
    """The provisional owner of posts a turn makes before its message exists.

    The column is ``String(36)`` and a run id is far longer, so this is the
    run's own uuid tail — unique per run, and the same value however often it is
    recomputed, which is the whole point: it is a dedupe key, not a reference.
    """
    return (run_id or "").rsplit(":", 1)[-1][:36]


async def _post_new_segments(
    *,
    storage: Any,
    group_id: str,
    profile: str,
    conversation_id: str,
    source_message_id: str,
    raw_text: str,
    mid_turn_breaks: Optional[List[Dict[str, Any]]],
    include_open_tail: bool,
) -> tuple[List[str], bool]:
    """Post whichever of this turn's segments have not been posted yet.

    Returns the ids posted now, and whether the turn had anything to say at all
    — which the caller needs kept apart: nothing to say is silence, while
    something to say that was all posted earlier is simply already said.

    Segment indices count only the segments that survive the silence filter, and
    that numbering is stable as a turn goes on: a segment is fixed by the flow
    break that closes it, and nothing later can change it or whether it was
    silent. So the index a segment gets mid-turn is the index it still has at
    the end, which is what lets the UNIQUE constraint recognise it.

    With ``include_open_tail`` false the text is cut at the last break, because
    everything after it is still being written. Cutting the text rather than
    dropping the last piece keeps the two paths literally the same computation:
    ``split_segments`` discards pieces that strip to nothing, so a piece's
    position in the list is not something a caller can count off the breaks.
    """
    text = raw_text or ""
    if not include_open_tail:
        closed_upto = max(
            (int(b.get("content_offset") or 0)
             for b in (mid_turn_breaks or []) if isinstance(b, dict)),
            default=0,
        )
        text = text[:closed_upto]
    # Notes off first, sentinel second. Every line a seat receives now ends
    # with a routing note, so an agent echoing the shape back is a matter of
    # time — and "[silent]\n[to: you]" does not reduce to the sentinel, so
    # the turn that meant to say nothing would post the word "[silent]" to
    # the room.
    segments = [
        stripped
        for seg, stripped in (
            (seg, strip_routing_notes(seg))
            for seg in split_segments(text, mid_turn_breaks)
        )
        if stripped and not is_silent_ignoring_notes(seg)
    ]
    if not segments:
        return [], False

    already = {
        int(row.get("segment") or 0)
        for row in await storage.find_by_source(source_message_id)
    }

    from app.groups.fanout import post_message
    from app.utils.agent_name import read_agent_name

    sender_name = read_agent_name(profile)
    posted: List[str] = []
    for index, segment_text in enumerate(segments):
        if index in already:
            continue
        row = await post_message(
            group_id=group_id,
            sender_kind="agent",
            sender_profile=profile,
            sender_name=sender_name,
            content=segment_text,
            source_conversation_id=conversation_id,
            source_message_id=source_message_id,
            segment=index,
            originated_from_shadow_turn=True,
        )
        if row is not None:
            posted.append(row["id"])
    return posted, True


async def on_shadow_turn_segment(
    *,
    conversation_id: str,
    profile: str,
    run_id: str,
    raw_text: str,
    mid_turn_breaks: Optional[List[Dict[str, Any]]] = None,
    context_id: Optional[str] = None,
) -> List[str]:
    """Post what a still-running turn has just finished saying.

    Called at each flow break, which is where an interrupted agent's reply ends.
    Only closed segments go out, and only ones not already posted — so a turn
    interrupted three times posts three times, once each, while the work carries
    on in between.

    Never raises: the room is a side effect of the turn, never a reason to fail
    it.
    """
    try:
        group_id = group_id_from_context(context_id)
        if not group_id or not (raw_text or "").strip():
            # Choosing not to answer an interruption is the common outcome in a
            # busy room, and it still emits a break. Answering it with a pair of
            # database reads, every time, is not free.
            return []

        from app.storage import get_group_chat_storage

        storage = get_group_chat_storage()
        group = await storage.get_group(group_id)
        if group is None or profile not in (group.get("members") or []):
            return []

        posted, _ = await _post_new_segments(
            storage=storage,
            group_id=group_id,
            profile=profile,
            conversation_id=conversation_id,
            source_message_id=live_source_key(run_id),
            raw_text=raw_text,
            mid_turn_breaks=mid_turn_breaks,
            include_open_tail=False,
        )
        if posted:
            logger.info(
                f"[group] {profile} answered mid-turn in {group_id} "
                f"({len(posted)} post(s))"
            )
        return posted
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] failed to post a mid-turn reply for {profile} "
            f"in {conversation_id}"
        )
        return []


async def on_shadow_turn_complete(
    *,
    conversation_storage: Any,
    conversation_id: str,
    profile: str,
    run_id: str,
    assistant_msg_id: Optional[str],
    raw_text: str,
    final_text: str,
    mid_turn_breaks: Optional[List[Dict[str, Any]]] = None,
    cancelled: bool = False,
    errored: bool = False,
    context_id: Optional[str] = None,
) -> List[str]:
    """Post a finished seat turn into its group. Returns the posted message ids.

    Anything the turn already said mid-turn is re-pointed at the message that has
    now persisted, and counted as posted — so an interrupted turn is stamped for
    what it really did, and the trace behind those interim posts is findable.

    Never raises: a room that cannot be posted to must not turn a completed turn
    into a failed one.
    """
    posted: List[str] = []
    marker: Dict[str, Any] = {"run_id": run_id}
    try:
        group_id = group_id_from_context(context_id)
        if not group_id:
            conv = await conversation_storage.get_conversation(conversation_id)
            group_id = group_id_from_context((conv or {}).get("context_id"))
        if not group_id:
            return []
        marker["group_id"] = group_id

        from app.storage import get_group_chat_storage

        storage = get_group_chat_storage()
        group = await storage.get_group(group_id)

        # Before any early return: whatever this turn said mid-turn is already
        # in the room, and it belongs to the message that just persisted however
        # the turn ended. Skipping this on the cancelled path would leave those
        # rows keyed by a run nobody will look up again.
        owner = assistant_msg_id or live_source_key(run_id)
        if assistant_msg_id:
            try:
                posted.extend(
                    await storage.rekey_source(
                        live_source_key(run_id), assistant_msg_id,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[group] could not re-point {profile}'s mid-turn posts "
                    f"in {conversation_id}"
                )

        if group is None or profile not in (group.get("members") or []):
            marker["kind"] = "skipped"
            marker["reason"] = "not_a_member"
            await _stamp(conversation_storage, assistant_msg_id, marker, posted)
            return posted

        if cancelled or errored:
            # Not "silent": a turn that spoke before it died said something the
            # room can see, and stamping it silent would drop that from its own
            # history too.
            marker["kind"] = "posted" if posted else "skipped"
            marker["reason"] = "cancelled" if cancelled else "errored"
            if errored:
                await _announce_failure(group_id, profile)
            await _stamp(conversation_storage, assistant_msg_id, marker, posted)
            return posted

        if (final_text or "").strip() in EMPTY_FINALS:
            marker["kind"] = "posted" if posted else "skipped"
            marker["reason"] = "empty"
            await _stamp(conversation_storage, assistant_msg_id, marker, posted)
            return posted

        fresh, had_something_to_say = await _post_new_segments(
            storage=storage,
            group_id=group_id,
            profile=profile,
            conversation_id=conversation_id,
            source_message_id=owner,
            raw_text=raw_text or final_text,
            mid_turn_breaks=mid_turn_breaks,
            include_open_tail=True,
        )
        posted.extend(fresh)
        if posted:
            marker["kind"] = "posted"
        elif had_something_to_say:
            # It had something to say and every piece of it was already in the
            # room. Not silence — saying so would drop this turn's history.
            marker["kind"] = "skipped"
            marker["reason"] = "duplicate"
        else:
            marker["kind"] = "silent"
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] failed to post the turn for {profile} in {conversation_id}"
        )
        marker.setdefault("kind", "skipped")
        marker["reason"] = "error"
    await _stamp(conversation_storage, assistant_msg_id, marker, posted)
    return posted


async def _stamp(
    conversation_storage: Any,
    assistant_msg_id: Optional[str],
    marker: Dict[str, Any],
    posted: List[str],
) -> None:
    """Record the outcome on the agent's message, always.

    An unstamped agent row in a seat means "this turn never reached the hook",
    which is exactly what the boot sweep looks for. Stamping even the silent and
    skipped cases is therefore not bookkeeping — it is what stops the sweep from
    re-posting things that were deliberately not posted.
    """
    if not assistant_msg_id:
        return
    marker = {**marker, "posted_message_ids": posted}
    try:
        await conversation_storage.update_message_metadata(
            assistant_msg_id, {"group": marker},
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] failed to stamp the group marker on {assistant_msg_id}"
        )


async def _announce_failure(group_id: str, profile: str) -> None:
    """Tell the room an agent's turn failed, without asking anyone to react."""
    try:
        from app.groups.fanout import post_message
        from app.utils.agent_name import read_agent_name

        await post_message(
            group_id=group_id,
            sender_kind="system",
            sender_name="Cremind",
            content=(
                f"{read_agent_name(profile)} hit an internal error and could not "
                "answer."
            ),
            deliver_only=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[group] could not announce a failed turn", exc_info=True)


def seat_event_payload(
    profile: str, conversation_id: str, event: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """One conversation-bus frame as a room frame, or ``None`` to drop it.

    Pure, so the allowlist can be tested without a bus on either side. The
    ``profile`` it stamps is what the SSE endpoint gates on: a member may watch
    its own agent work, and only the admin may watch everyone's.

    ``seat_seq`` is the frame's sequence number on the SEAT's bus, forwarded so
    a client joining mid-turn can tell a catch-up frame from the live one that
    repeats it. It is deliberately not the group stream's ``seq`` — it lives
    under its own key precisely so the two never collide — and both producers
    hand this function whole bus frames, so it is always there in practice.
    """
    if not isinstance(event, dict) or not profile or not conversation_id:
        return None
    event_type = event.get("type")
    if event_type not in SEAT_EVENT_TYPES:
        return None
    return {
        "profile": profile,
        "conversation_id": conversation_id,
        "type": event_type,
        "seat_seq": event.get("seq"),
        "data": event.get("data") or {},
    }


async def bind_seat_mirror(
    conv: Optional[Dict[str, Any]], profile: str,
) -> Optional[Callable[[Dict[str, Any]], Any]]:
    """Start mirroring a seat's live frames onto its room. Returns the handle.

    A seat turn already publishes everything the room wants to show — which tool
    it called, what came back, where it is working — but only to the profile that
    owns the seat, and the room watches one group stream rather than a
    conversation stream per member (a browser will not hold six SSE connections
    for a five-member room). So the frames are tapped at the conversation bus and
    re-published on the group bus, tagged with the member they came from.

    ``None`` for anything that is not a seat, so the caller can invoke it
    unconditionally; never raises, because a room that cannot show the steps must
    not cost the member its turn. Pass the returned handle to
    :func:`unbind_seat_mirror` in the turn's ``finally``.
    """
    try:
        group_id = group_id_from_context((conv or {}).get("context_id"))
        conversation_id = (conv or {}).get("id") or ""
        if not group_id or not conversation_id or not profile:
            return None

        from app.events import get_event_stream_bus
        from app.groups.bus import get_group_stream_bus

        async def _tap(event: Dict[str, Any]) -> None:
            payload = seat_event_payload(profile, conversation_id, event)
            if payload is None:
                return
            # Ephemeral: a turn's step frames must never crowd the room's own
            # messages out of the replay ring. A client that connects mid-turn
            # is caught up from the seat's ring instead (see the group stream
            # endpoint), which is the authoritative copy anyway.
            await get_group_stream_bus().publish(
                group_id, "seat_event", payload, ephemeral=True,
            )

        await get_event_stream_bus().add_tap(conversation_id, _tap)
        logger.info(
            f"[group] seat mirror bound: {profile} in {group_id} "
            f"({conversation_id})"
        )
        return _tap
    except Exception:  # noqa: BLE001
        # Warning, not debug: the file sink is INFO, so a bind that fails here
        # is the difference between a room that shows its agents working and one
        # that shows nothing at all — and at debug it left no trace to find.
        logger.warning("[group] could not bind the seat mirror", exc_info=True)
        return None


async def unbind_seat_mirror(
    conversation_id: str, tap: Optional[Callable[[Dict[str, Any]], Any]],
) -> None:
    """Stop mirroring. A tap left attached would follow the *next* turn too,
    reporting its steps against a run the room already saw finish."""
    if tap is None or not conversation_id:
        return
    try:
        from app.events import get_event_stream_bus

        await get_event_stream_bus().remove_tap(conversation_id, tap)
    except Exception:  # noqa: BLE001
        logger.warning("[group] could not unbind the seat mirror", exc_info=True)


async def publish_agent_status(
    *,
    conv: Optional[Dict[str, Any]],
    profile: str,
    state: str,
) -> None:
    """Broadcast "X is thinking…" / "X is idle" for a seat turn.

    A no-op for every conversation that is not a seat, so the caller can invoke
    it unconditionally on the hot path.

    The room is checked for the same reason :func:`on_shadow_turn_complete`
    checks it: deleting a group does not stop the turns already running in its
    seats, and both hooks fire afterwards. The turn-end hook already refuses to
    post into a room that is gone; this one would instead re-create the ring and
    the sequence counter that ``GroupStreamBus.discard`` had just popped — for a
    group nobody will ever subscribe to and nobody will ever discard again.
    """
    try:
        group_id = group_id_from_context((conv or {}).get("context_id"))
        if not group_id or not await _room_exists(group_id):
            return
        from app.groups.bus import get_group_stream_bus
        from app.utils.agent_name import read_agent_name

        await get_group_stream_bus().publish(group_id, "agent_status", {
            "profile": profile,
            "agent_name": read_agent_name(profile),
            "state": state,
        })
    except Exception:  # noqa: BLE001
        # A dropped status is why a room shows nobody thinking (or an agent
        # thinking forever), so it must be findable in the log at INFO.
        logger.warning("[group] failed to publish agent status", exc_info=True)


async def _room_exists(group_id: str) -> bool:
    """Whether the group is still there — ``True`` when we cannot tell.

    Fail-open on purpose: swallowing an ``idle`` because one indexed lookup
    happened to fail would leave that member showing as thinking in every open
    room view until somebody reloaded. A stray frame is the cheaper mistake.
    """
    try:
        from app.storage import get_group_chat_storage

        return await get_group_chat_storage().group_exists(group_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            f"[group] could not confirm {group_id} still exists", exc_info=True,
        )
        return True
