"""Shared teardown for deleting a channel client outright.

"Delete this client" means more than dropping a row: the goal is that their next
message is indistinguishable from a first-ever contact. That spans four places
the person left a mark —

1. **Their conversation**, which owns the messages and the automations homed on
   it. Handled by the existing conversation teardown
   (:func:`app.reset._conversations.cleanup_conversation_dependents`) so bound
   skill events / file watchers / schedules are disarmed in the live managers
   rather than CASCADE-deleted behind their backs.
2. **Files and in-memory state keyed by that conversation** — uploaded
   attachments, the per-conversation context bucket (working-directory
   override, current shell directory), the reasoning context and any agent
   activity snapshot. None of these are reached by a conversation delete today,
   so they are cleaned here explicitly.
3. **The channel's own config.** On a notification channel the person may also
   be listed statically in ``target_chat_ids``, and that list is consulted
   independently of the sender table — leave it and they keep receiving pushes
   after being "deleted", which is the one leftover that would be actively
   wrong rather than merely untidy.
4. **The sender row**, last, once everything pointing at it is gone.

Two things deliberately survive, because "forget the person" is not the same as
"rewrite history":

- ``usage_records`` — the tokens were really spent and the money really left the
  account, so the spend stays in the profile totals. Its ``conversation_id`` FK
  is ``ON DELETE SET NULL``, so the rows keep the cost but lose every link back
  to the person: nothing identifying remains.
- Long-term memories are removed only when they were learned *from that
  conversation*. Facts the profile holds for other reasons are not the client's
  to erase. Both memory backends are swept — the SQL queue and the vector
  collection — since which one holds the facts depends on whether embedding is
  enabled, and recall is filtered by profile alone, so a fact left in either
  store would resurface in a stranger's conversation.

Lives in :mod:`app.reset` next to ``_conversations`` so the channels API, the
CLI and any future clean component all run one sequence instead of three
divergent ones.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger


async def delete_sender_completely(
    conversation_storage: ConversationStorage,
    *,
    channel: dict,
    sender: dict,
    adapter: Any | None = None,
) -> dict:
    """Erase one channel client. Returns a summary of what was removed.

    ``channel`` and ``sender`` are the already-validated rows (the caller owns
    the 404/403/409 decisions). ``adapter`` is the live adapter for this channel
    when one is running, so its in-memory state for the sender can be dropped
    too; ``None`` is fine — a stopped adapter holds nothing.

    Every step is best-effort and logged rather than raised, except the sender
    delete itself: a partial clean must not leave the row behind and report
    failure, because the operator's next move would be to retry a delete that
    now 404s.
    """
    profile = channel["profile"]
    channel_id = channel["id"]
    sender_id = sender["sender_id"]
    conv_id = sender.get("conversation_id")

    summary: dict[str, Any] = {
        "sender_id": sender_id,
        "conversation_id": conv_id,
        "deleted_messages": 0,
        "forgot_memories": 0,
        "unsubscribed_target": False,
    }

    if conv_id:
        from app.reset._conversations import cleanup_conversation_dependents

        await cleanup_conversation_dependents(conversation_storage, conv_id)
        summary["deleted_messages"] = await _clear_messages(
            conversation_storage, conv_id,
        )
        summary["forgot_memories"] = await _forget_conversation_memories(
            profile, conv_id,
        )
        _remove_uploads(profile, conv_id)
        _clear_in_memory_context(conv_id)
        try:
            await conversation_storage.delete_conversation(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"delete client: conversation delete failed {conv_id}")

    summary["unsubscribed_target"] = await _prune_target_chat_id(
        conversation_storage, channel, sender_id, adapter=adapter,
    )

    deleted = await conversation_storage.delete_sender(sender["id"])
    summary["deleted"] = bool(deleted)

    if adapter is not None:
        try:
            adapter.forget_sender(sender_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"delete client: forget_sender failed {sender_id}")

    logger.info(
        f"channels: deleted client {sender_id} on channel {channel_id} "
        f"(conversation={conv_id}, messages={summary['deleted_messages']}, "
        f"memories={summary['forgot_memories']})"
    )
    return summary


async def _clear_messages(
    conversation_storage: ConversationStorage, conversation_id: str,
) -> int:
    """Delete the conversation's messages, returning an exact count.

    Clearing before the conversation row goes is what makes the number
    reportable — ``get_messages`` paginates, so counting through it would
    silently cap at one page.
    """
    try:
        return await conversation_storage.clear_conversation_messages(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"delete client: message clear failed {conversation_id}",
        )
        return 0


async def _forget_conversation_memories(profile: str, conversation_id: str) -> int:
    """Drop long-term facts the agent learned from this conversation.

    Both stores are swept because long-term memory lives in exactly one of them
    depending on configuration: with embedding enabled the facts are vector
    points and the DB queue is empty, without it the reverse. Purging only the
    DB would silently leave a deleted client's facts in the vector store, where
    recall is filtered by profile alone — so they would resurface in an
    unrelated conversation's prompt. Each sweep is independent; a failure in one
    must not skip the other.
    """
    removed = 0
    try:
        from app.storage import get_memory_storage

        removed += await get_memory_storage().delete_by_source_conversation(
            profile, conversation_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"delete client: DB memory cleanup failed {conversation_id}",
        )
    try:
        from types import SimpleNamespace

        from app.agent import memory_vectorstore
        from app.config.embedding_state import embedding_state

        shim = SimpleNamespace(
            embedding=embedding_state.embedding,
            vector_store=embedding_state.vector_store,
        )
        # Vector calls are synchronous network/disk IO; keep them off the loop.
        removed += await asyncio.to_thread(
            lambda: memory_vectorstore.forget_conversation(
                agent=shim, profile=profile, conversation_id=conversation_id,
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"delete client: vector memory cleanup failed {conversation_id}",
        )
    return removed


def _remove_uploads(profile: str, conversation_id: str) -> None:
    """Remove attachments the client uploaded into this conversation.

    Nothing else reaches these: the uploads directory is only wiped wholesale at
    boot or pruned when idle, so a per-conversation delete has to do it here or
    the files outlive the person.
    """
    try:
        from app.utils.uploads_tmp import conversation_tmp_dir

        shutil.rmtree(conversation_tmp_dir(profile, conversation_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.debug("delete client: uploads cleanup failed", exc_info=True)


def _clear_in_memory_context(conversation_id: str) -> None:
    """Forget per-conversation runtime state (working dir, contexts, activity)."""
    try:
        from app.utils.context_storage import clear_context

        clear_context(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("delete client: context clear failed", exc_info=True)
    try:
        from app.agent.context_store import ReasoningContextStore

        ReasoningContextStore().clear_context(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("delete client: reasoning context clear failed", exc_info=True)
    try:
        from app.agent import agent_activity

        agent_activity.clear(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("delete client: activity clear failed", exc_info=True)


async def _prune_target_chat_id(
    conversation_storage: ConversationStorage, channel: dict, sender_id: str,
    *, adapter: Any | None = None,
) -> bool:
    """Remove ``sender_id`` from a notification channel's static recipient list.

    ``config.target_chat_ids`` is consulted independently of the sender table,
    so a client listed there would keep receiving notifications after being
    deleted — the one leftover that is actively wrong rather than untidy.

    The running adapter's copy of the config is patched as well as the stored
    row: ``_notification_recipients`` reads ``self.channel["config"]`` from
    memory, so a database-only prune would keep delivering to them until the
    channel next restarted. (Subscriber-based delivery needs no such patch — it
    re-reads the sender table on every send, so deleting the row stops it at
    once.)

    Returns True when the list actually changed.
    """
    config = dict(channel.get("config") or {})
    raw = config.get("target_chat_ids")
    if raw is None:
        return False

    if isinstance(raw, str):
        current = [part.strip() for part in raw.split(",") if part.strip()]
        was_csv = True
    elif isinstance(raw, (list, tuple)):
        current = [str(item).strip() for item in raw if str(item).strip()]
        was_csv = False
    else:
        return False

    remaining = [item for item in current if item != sender_id]
    if len(remaining) == len(current):
        return False

    pruned = ",".join(remaining) if was_csv else remaining
    config["target_chat_ids"] = pruned
    try:
        await conversation_storage.update_channel(channel["id"], config=config)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"delete client: could not prune target_chat_ids on {channel['id']}",
        )
        return False

    if adapter is not None:
        try:
            live = getattr(adapter, "channel", None)
            if isinstance(live, dict):
                live_config = dict(live.get("config") or {})
                live_config["target_chat_ids"] = pruned
                live["config"] = live_config
        except Exception:  # noqa: BLE001
            logger.debug("delete client: live config patch failed", exc_info=True)
    return True
