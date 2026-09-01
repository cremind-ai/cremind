"""Delivery of user messages that arrive while a turn is already running.

A message sent mid-turn used to wait: the web UI blocked the send button, the
channels answered "I'm thinking…" and dropped it, and the API quietly queued it
behind the running turn. All three meant the agent could not know about it until
it had finished doing something the message might have changed or cancelled.

Here it is handed to the running turn instead. The message is persisted first —
so the bubble appears immediately and the record exists no matter what follows —
then parked; the reasoning loop drains it at the top of its next step, where it
becomes an ordinary ``role:"user"`` message in the turn's context.

Three functions, one for each way a parked message can end:

:func:`try_park_user_message`
    Persist + park. Returns None for an idle conversation (the caller runs the
    message as a normal turn), and an outcome saying ``injected`` or not.
:func:`flush_user_inbox`
    Turn-end reconciliation: whatever the turn did not absorb runs as one
    follow-up turn. The injection is an optimisation; this is the guarantee.
:func:`sweep_stranded_mid_turn_messages`
    Boot sweep. The park state is in memory, so a crash mid-turn leaves rows at
    ``mid_turn.state == "pending"`` — which history deliberately hides. Without
    this they would be invisible forever, so it is required, not a nicety.

**The three states** live in ``messages.metadata.mid_turn.state`` (a JSON column,
so no migration): ``pending`` while parked, then exactly one of ``consumed`` (a
turn absorbed it; its trace replays the text) or ``released`` (it runs as its own
turn, an ordinary user row). :func:`app.utils.common.convert_db_messages_to_history`
reads that state so exactly one copy of the message ever reaches the model.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from app.events import task_result_inbox
from app.utils.logger import logger


class ParkOutcome:
    """What happened to a message offered to a possibly-busy conversation.

    ``injected`` means it is in the running turn's hands. Otherwise the row is
    persisted and ``released`` and the caller must enqueue a turn for it, passing
    ``message_id`` as ``existing_user_message_id`` so it is not persisted twice.
    """

    __slots__ = ("injected", "message_id", "run_id", "agent_text")

    def __init__(
        self,
        *,
        injected: bool,
        message_id: str,
        run_id: Optional[str] = None,
        agent_text: str = "",
    ) -> None:
        self.injected = injected
        self.message_id = message_id
        self.run_id = run_id
        self.agent_text = agent_text


def _storage() -> Any:
    from app.events import runner as event_runner
    return event_runner.get_conversation_storage()


async def _set_state(
    conversation_storage: Any, message_id: str, state: str, run_id: Optional[str],
) -> None:
    """Move a mid-turn row to a terminal state. Whole-object patch: the metadata
    merge is shallow, so writing a partial ``mid_turn`` would drop its siblings."""
    try:
        await conversation_storage.update_message_metadata(
            message_id, {"mid_turn": {"state": state, "run_id": run_id}},
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[user_msg] failed to mark message {message_id} {state}"
        )


async def try_park_user_message(
    *,
    conversation_id: str,
    profile: str,
    query: str,
    user_message_metadata: Optional[Dict[str, Any]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    mode: str = "reasoning",
    reasoning: bool = True,
    event_run_id: Optional[str] = None,
    event_run: bool = False,
) -> Optional[ParkOutcome]:
    """Offer a user message to a conversation that may have a turn in flight.

    Returns ``None`` when the conversation is idle — nothing is persisted and the
    caller's normal enqueue path runs unchanged, exactly as before this feature.

    Otherwise the row is persisted and published on the bus BEFORE the park is
    attempted, so the message is on the record and on every open client the
    moment it is accepted, whichever way the park then goes.
    """
    if not conversation_id or not str(query or "").strip():
        return None
    conversation_storage = _storage()
    if conversation_storage is None:
        logger.error("[user_msg] conversation storage not initialized; cannot park")
        return None

    # Cheap pre-check. The turn may end before the park below, which is exactly
    # why the park itself re-checks atomically.
    active_run = task_result_inbox.bound_run_for(conversation_id)
    if not active_run:
        return None

    from app.agent.stream_runner import (
        _append_attachments_note, attachment_file_parts,
    )
    from app.utils.message_tokens import resolve_message_tokens

    metadata = {
        **(user_message_metadata or {}),
        "mid_turn": {"state": "pending", "run_id": active_run},
    }
    try:
        row = await conversation_storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=query,
            parts=attachment_file_parts(attachments) or None,
            metadata=metadata,
        )
        message_id = row.get("id") if isinstance(row, dict) else None
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[user_msg] failed to persist mid-turn message for {conversation_id}"
        )
        return None
    if not message_id:
        return None

    # Same frame a normal turn publishes, so every open client renders the bubble
    # the same way; ``injected`` tells them no new run is starting.
    try:
        from app.events.stream_bus import get_event_stream_bus
        await get_event_stream_bus().publish(conversation_id, "user_message", {
            "id": message_id,
            "content": query,
            "metadata": metadata,
            "injected": True,
        })
    except Exception:  # noqa: BLE001
        logger.exception(f"[user_msg] failed to publish user_message for {conversation_id}")

    # What the model sees: rendered tokens plus the attachment-path note, exactly
    # like the normal path (the persisted row keeps the raw text and its chips).
    try:
        agent_text = await resolve_message_tokens(
            query, profile=profile, conversation_storage=conversation_storage,
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[user_msg] token resolution failed for {conversation_id}")
        agent_text = query
    agent_text = _append_attachments_note(agent_text, attachments)

    payload = {
        "message_id": message_id,
        "text": query,
        "agent_text": agent_text,
        "attachments": attachments or None,
        "mode": mode,
        "reasoning": reasoning,
        "event_run": event_run,
        "event_run_id": event_run_id,
        "addressed": _addressed_to_us(user_message_metadata),
        "ts": time.time(),
    }
    parked_run = task_result_inbox.park_user_message_if_bound(conversation_id, payload)
    if parked_run:
        logger.info(
            f"[user_msg] parked mid-turn message {message_id} into run {parked_run}"
        )
        return ParkOutcome(
            injected=True, message_id=message_id, run_id=parked_run,
            agent_text=agent_text,
        )

    # Lost the race (the turn ended) or the inbox is full: the row exists, so
    # release it and let the caller run it as an ordinary turn. Never a drop.
    await _set_state(conversation_storage, message_id, "released", active_run)
    return ParkOutcome(
        injected=False, message_id=message_id, agent_text=agent_text,
    )


def _addressed_to_us(metadata: Optional[Dict[str, Any]]) -> bool:
    """Whether this message was aimed at THIS agent, decided before it got here.

    In a one-to-one conversation the answer is always yes and nobody asks. In a
    room it has already been decided twice over by the time the message is
    parked, and the answer is recorded on the row:

    * a Cremind room routes every post, and a member the router did not name is
      quiet-written rather than parked — so ``routed_away`` false on a row that
      reached the inbox means the router picked this seat;
    * a platform group parks only what its mention check or relevance judge
      passed, and ``mentioned`` says which.

    Read at the pause, this is what separates "the agent chose not to answer me"
    from "the agent was never asked" — see ``_GROUP_ACK_INSIST``.
    """
    if not isinstance(metadata, dict):
        return False
    room = metadata.get("group")
    if isinstance(room, dict) and not room.get("routed_away"):
        return True
    group = metadata.get("channel_group")
    if isinstance(group, dict) and group.get("mentioned"):
        return True
    return False


def _continuation_query(
    payloads: List[Dict[str, Any]], *, group_chat: bool = False,
) -> str:
    """Prompt for the follow-up turn.

    Deliberately does NOT repeat the message text: the rows are released before
    this runs, so they are already the newest user messages in the history the
    turn is given. Repeating them would feed the model the same words twice.

    In a group chat the instruction flips from "address it" to "decide whether it
    is for you". Everything posted to a room reaches every member, so most of
    what a turn missed was somebody else's business — an unconditional "address
    them now" would make all three agents answer a question asked of one.
    """
    count = len(payloads)
    noun = "message" if count == 1 else f"{count} messages"
    if group_chat:
        return (
            f"[Unread group {noun}] "
            f"{'A message was' if count == 1 else f'{count} messages were'} "
            "posted to the group while your previous turn was ending, so it "
            f"could not take {'it' if count == 1 else 'them'} into account. "
            f"{'It is' if count == 1 else 'They are'} the most recent "
            f"{'message' if count == 1 else 'messages'} above. Decide whether "
            f"{'it needs' if count == 1 else 'any of them need'} an answer from "
            "you and reply if so; if none does, answer exactly [silent]."
        )
    lines = [
        f"[Unanswered {noun}] The user sent {'a message' if count == 1 else noun} "
        "while the previous turn was running, and that turn ended without "
        f"addressing {'it' if count == 1 else 'them'}. "
        f"{'It is' if count == 1 else 'They are'} the most recent user "
        f"{'message' if count == 1 else 'messages'} in the conversation above. "
        f"Address {'it' if count == 1 else 'them'} now."
    ]
    paths = [
        p for payload in payloads
        for p in [
            a.get("path") for a in (payload.get("attachments") or [])
            if isinstance(a, dict) and a.get("path")
        ]
    ]
    if paths:
        lines.append("")
        lines.append("[Attached files — absolute paths:]")
        lines += [f"- {p}" for p in paths]
    return "\n".join(lines)


async def _conversation_flags(
    conversation_storage: Any, conversation_id: str, profile: str,
) -> tuple[Optional[str], bool, bool]:
    """Read what this conversation needs from the follow-up turn.

    Returns ``(event_run_id, is_event_run, is_group_chat)``. A hidden event-run
    conversation is also resumed here (status back to ``running``), mirroring the
    chat POST path; a group seat needs nothing resumed, only different wording.
    """
    try:
        conv = await conversation_storage.get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[user_msg] failed to load conversation {conversation_id}")
        return None, False, False
    kind = (conv or {}).get("kind")
    # A platform group is an ordinary ``chat`` conversation, so the kind alone
    # cannot spot it — but its follow-up turn needs the same wording as a seat's:
    # several people are talking, and staying silent is a real answer.
    from app.channels.groups.origin import is_channel_group_context

    is_group_chat = kind == "group_chat" or is_channel_group_context(
        (conv or {}).get("context_id")
    )
    if conv is None or kind != "event_run":
        return None, False, is_group_chat
    try:
        from app.storage import get_event_run_storage
        store = get_event_run_storage()
        run = await store.get_by_conversation(conversation_id)
        if run is None:
            return None, True, False
        await store.update_status(run["id"], status="running", clear_pending=True)
        from app.events.event_runs_admin_bus import publish_event_runs_changed
        publish_event_runs_changed(profile)
        return run["id"], True, False
    except Exception:  # noqa: BLE001
        logger.exception(f"[user_msg] failed to resume event run for {conversation_id}")
        return None, True, False


async def flush_user_inbox(*, conversation_id: str, profile: str) -> bool:
    """Run every unabsorbed mid-turn message as ONE follow-up turn.

    Rows are released FIRST, before anything that can fail: a released row is a
    normal part of the conversation, so even if the enqueue below dies the
    message is never lost — it is simply answered on the user's next turn rather
    than proactively.
    """
    payloads = task_result_inbox.take_unconsumed_user_messages(conversation_id)
    if not payloads:
        return False
    return await _deliver_followup(
        conversation_id=conversation_id, profile=profile, payloads=payloads,
    )


async def _deliver_followup(
    *, conversation_id: str, profile: str, payloads: List[Dict[str, Any]],
) -> bool:
    """Release the rows, then enqueue one turn to answer them."""
    conversation_storage = _storage()
    if conversation_storage is None:
        logger.error("[user_msg] conversation storage not initialized; cannot flush")
        return False

    for payload in payloads:
        message_id = payload.get("message_id")
        if message_id:
            await _set_state(
                conversation_storage, message_id, "released",
                (payload.get("run_id") if isinstance(payload, dict) else None),
            )

    event_run_id, is_event_run, is_group_chat = await _conversation_flags(
        conversation_storage, conversation_id, profile,
    )
    # Fall back to the flags the sender recorded (the conversation lookup only
    # knows it is an event run, not which run a reply was aimed at).
    if is_event_run and not event_run_id:
        event_run_id = next(
            (p.get("event_run_id") for p in reversed(payloads) if p.get("event_run_id")),
            None,
        )

    from app.events import queue as event_queue
    from app.events.event_task_delivery import _load_history

    history_messages = await _load_history(
        conversation_storage, conversation_id, profile,
    )

    # Mirror the continuation to the platform when this conversation lives on an
    # external channel: the adapter's forwarder for the finished run has already
    # terminated, so the follow-up needs one of its own.
    try:
        from app.events.run_dispatcher import _maybe_forward_to_channel
        await _maybe_forward_to_channel(
            conversation_storage, conversation_id, conversation_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[user_msg] channel forwarder setup failed")

    newest = payloads[-1]
    mode = str(newest.get("mode") or "reasoning")
    if mode == "plan":
        # A plan-mode turn is a fresh planning cycle the user asked for
        # explicitly; a continuation is not the place to start one.
        mode = "reasoning"

    from app.agent.stream_runner import make_run_id

    try:
        await event_queue.enqueue_user_message(
            conversation_id=conversation_id,
            run_id=make_run_id(conversation_id, kind="msg"),
            profile=profile,
            query=_continuation_query(payloads, group_chat=is_group_chat),
            history_messages=history_messages,
            reasoning=bool(newest.get("reasoning", True)),
            mode=mode,
            push_user_message=False,
            update_title_from_query=False,
            event_run_id=event_run_id,
            event_run=is_event_run,
            publish_notification=is_event_run,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[user_msg] follow-up enqueue failed for {conversation_id}; "
            f"{len(payloads)} message(s) stay in the conversation and will be "
            "answered on the next turn"
        )
        return False
    logger.info(
        f"[user_msg] queued a follow-up turn for {len(payloads)} unanswered "
        f"message(s) in {conversation_id}"
    )
    return True


def _attachments_from_parts(parts: Any) -> Optional[List[Dict[str, Any]]]:
    """Rebuild ``[{"name", "path"}]`` attachments from a persisted row's parts.

    The park payload lives in memory, so after a crash the file parts on the
    persisted row are the only record of what was attached. Paths that no
    longer exist are dropped — the uploads_tmp tree may have been wiped on
    this same boot — which still beats the old behaviour of dropping all of
    them unconditionally (files the agent had already moved out survive).
    """
    result: List[Dict[str, Any]] = []
    for part in parts or []:
        if not isinstance(part, dict) or part.get("kind") != "file":
            continue
        info = part.get("file") or {}
        path = str(info.get("uri") or "")
        if not path or not os.path.isfile(path):
            continue
        result.append({"name": info.get("name") or os.path.basename(path), "path": path})
    return result or None


async def sweep_stranded_mid_turn_messages() -> int:
    """Release mid-turn rows a crash left parked, and answer them.

    The park state is in memory: a hard stop mid-turn leaves rows at ``pending``,
    which history hides on purpose. Releasing them is what makes them visible
    again; the follow-up turn is what makes the answer arrive without the user
    having to poke the conversation.
    """
    conversation_storage = _storage()
    if conversation_storage is None:
        return 0
    try:
        rows = await conversation_storage.list_pending_mid_turn_messages()
    except Exception:  # noqa: BLE001
        logger.exception("[user_msg] boot sweep could not read stranded messages")
        return 0
    if not rows:
        return 0

    by_conversation: Dict[str, List[dict]] = {}
    for row in rows:
        by_conversation.setdefault(row.get("conversation_id") or "", []).append(row)

    swept = 0
    for conversation_id, conv_rows in by_conversation.items():
        if not conversation_id:
            continue
        try:
            conv = await conversation_storage.get_conversation(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"[user_msg] boot sweep failed to load {conversation_id}")
            continue
        if conv is None:
            continue
        profile = conv.get("profile") or ""
        payloads: List[Dict[str, Any]] = []
        for row in conv_rows:
            message_id = row.get("id")
            if not message_id:
                continue
            payloads.append({
                "message_id": message_id,
                "text": row.get("content") or "",
                "agent_text": row.get("content") or "",
                "attachments": _attachments_from_parts(row.get("parts")),
                "mode": "reasoning",
                "reasoning": True,
                "event_run": False,
                "event_run_id": None,
            })
        if not payloads:
            continue
        swept += len(payloads)
        # Same delivery path as a turn-end flush, so the wording, the channel
        # forwarding and the event-run handling are identical.
        try:
            await _deliver_followup(
                conversation_id=conversation_id, profile=profile, payloads=payloads,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[user_msg] boot sweep flush failed for {conversation_id}"
            )
    if swept:
        logger.info(f"[user_msg] boot sweep released {swept} stranded message(s)")
    return swept
