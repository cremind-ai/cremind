"""Getting a group message to the agent — and the reply back to the group.

One conversation per approved group, and it is an ordinary ``kind="chat"`` row
bound to the channel, exactly like a DM sender's. That is a choice: a hidden
conversation (what Cremind's own rooms use for seats) would keep the sidebar
tidy but leave no way to watch the agent talking to real people, or to step in
from the web composer when it says something you would rather it had not.

Delivery is the same park-then-enqueue shape as an inbound DM
(:meth:`app.channels.base.BaseChannelAdapter._dispatch_to_agent`) and as a
Cremind room's fan-out: try to fold the message into a turn already running,
otherwise start one. A group is a place where several people talk at once, so
"somebody said something while you were mid-sentence" is the normal case here,
and folding it in is what lets one reply cover everything that arrived instead
of the agent answering a stale question and then answering again.

A message the agent should not answer is written to the same conversation with
no turn started. It costs one row and it is the difference between an agent that
knows what the room has been talking about and one that reads every question it
IS asked without any of its context.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.channels.groups.origin import channel_group_context_id
from app.utils.logger import logger

# How much of the conversation is replayed into a turn. Read from the frontier
# (``newest_first``) rather than with ``get_messages``, which returns the OLDEST
# rows — in a long-running group that would hand the model the first hundred
# messages of a thread it has been in for weeks.
_HISTORY_ROWS = 200


async def ensure_group_conversation(
    adapter: Any, group: Dict[str, Any],
) -> Optional[str]:
    """The conversation this group's messages land in, created on first use.

    Resolution order — the stored pointer, then a ``context_id`` lookup, then
    create — so a pointer lost to a manual delete self-heals instead of spawning
    a second conversation for the same group.
    """
    group_id = group.get("id")
    if not group_id:
        return None
    storage = adapter.storage
    conv_id = group.get("conversation_id")
    if conv_id:
        try:
            if await storage.get_conversation(conv_id):
                return conv_id
        except Exception:  # noqa: BLE001
            logger.debug(
                f"[channel_group] could not read conversation {conv_id}",
                exc_info=True,
            )

    context_id = channel_group_context_id(group_id)
    title = (group.get("title") or "").strip() or str(
        group.get("platform_chat_id") or "Group chat"
    )
    conv = None
    try:
        conv = await storage.get_conversation_by_context(adapter.profile, context_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[channel_group] context lookup failed; creating a conversation",
            exc_info=True,
        )
    if conv is None:
        conv = await storage.create_conversation(
            profile=adapter.profile,
            context_id=context_id,
            title=title[:256],
            channel_id=adapter.channel_id,
            kind="chat",
        )
        _publish_conversations_changed(adapter.profile)

    conversation_id = (conv or {}).get("id")
    if not conversation_id:
        return None

    from app.storage import get_channel_group_storage

    await get_channel_group_storage().update_group(
        group_id, conversation_id=conversation_id,
    )
    group["conversation_id"] = conversation_id
    return conversation_id


async def deliver_to_group(
    adapter: Any,
    group: Dict[str, Any],
    conversation_id: str,
    rendered: str,
    metadata: Dict[str, Any],
    *,
    start_turn: bool,
) -> None:
    """Hand one group message to the agent, with or without starting a turn."""
    if not start_turn:
        await _quiet_write(adapter, conversation_id, rendered, metadata)
        return

    from app.agent.stream_runner import make_run_id
    from app.channels.reply_target import group_target
    from app.events import queue as event_queue
    from app.events import user_message_delivery

    target = group_target(group)

    parked = await user_message_delivery.try_park_user_message(
        conversation_id=conversation_id,
        profile=adapter.profile,
        query=rendered,
        user_message_metadata=metadata,
    )
    if parked is not None and parked.injected:
        # Folded into the running turn — that turn's answer covers it. The run
        # may have been started somewhere with no forwarder pointed at this
        # group (a schedule, a task result), so make sure one is.
        adapter.ensure_group_forwarder(target, conversation_id)
        return

    history = await _history(adapter, conversation_id)
    adapter.expect_run_for(target, conversation_id)
    try:
        await event_queue.enqueue_user_message(
            conversation_id=conversation_id,
            run_id=make_run_id(conversation_id, kind="cgroup"),
            profile=adapter.profile,
            query=rendered,
            history_messages=history,
            reasoning=True,
            user_message_metadata=metadata,
            # A message that was parked and then lost the race to the turn's end
            # is already persisted; run it without persisting it twice.
            push_user_message=parked is None,
            existing_user_message_id=parked.message_id if parked is not None else None,
            update_title_from_query=False,
        )
    except Exception:  # noqa: BLE001
        # No apology into the room: everyone in a real group would read it, and
        # an internal error is not their business. The log is where this belongs.
        adapter.release_run_for(target)
        logger.exception(
            f"[channel_group] could not enqueue a message for {conversation_id}"
        )


async def _quiet_write(
    adapter: Any, conversation_id: str, rendered: str, metadata: Dict[str, Any],
) -> None:
    """Store a message as context without waking the agent.

    Written straight to the row rather than parked: parking would hand it to the
    turn-end flush, which would start exactly the turn this path exists to avoid.
    """
    try:
        row = await adapter.storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=rendered,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] could not store a quiet message in {conversation_id}"
        )
        return
    # Best-effort, so a web client watching the group sees the traffic the agent
    # is choosing not to answer rather than an inexplicably idle thread.
    #
    # Its own frame type, and transient. A ``user_message`` frame means "a run
    # is starting" to every client there is — the web store flips the
    # conversation into its streaming state on one, and only a terminal frame
    # clears that. No run is starting here and no terminal frame is coming, so
    # sending one leaves the reader looking at "Agent is thinking…" forever;
    # keeping it in the replay ring re-arms that for every later subscriber too.
    try:
        from app.events import get_event_stream_bus

        await get_event_stream_bus().publish_transient(
            conversation_id,
            "quiet_user_message",
            {
                "id": (row or {}).get("id"),
                "content": rendered,
                "metadata": metadata,
            },
            profile=adapter.profile,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[channel_group] could not publish a quiet message", exc_info=True,
        )


async def _history(adapter: Any, conversation_id: str) -> list:
    """The recent tail of the conversation, as model history."""
    try:
        from app.config.user_config import replay_reasoning_enabled
        from app.utils.common import convert_db_messages_to_history

        rows = await adapter.storage.get_messages_after(
            conversation_id, -1, limit=_HISTORY_ROWS, newest_first=True,
        )
        if not rows:
            return []
        return convert_db_messages_to_history(
            rows, include_reasoning=replay_reasoning_enabled(adapter.profile),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] could not load history for {conversation_id}"
        )
        return []


def _publish_conversations_changed(profile: str) -> None:
    try:
        from app.events.conversations_list_bus import publish_conversations_changed

        publish_conversations_changed(profile)
    except Exception:  # noqa: BLE001
        logger.debug("[channel_group] could not nudge the sidebar", exc_info=True)
