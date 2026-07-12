"""Shared conversation-teardown helpers, extracted from ``app.api.conversations``.

Deleting a conversation is not a single ``DELETE`` — anything *homed* on it must be
torn down first (bound event/watcher/schedule rules and their run rows + hidden run
conversations), plus this conversation's own in-memory queue/stream state and any saved
Plan-mode files. That logic used to live only as a closure inside
:func:`app.api.conversations.get_conversation_routes`; it is lifted here so both the
conversations API and the per-profile clean engine (:mod:`app.reset.engine`) reuse the
exact same sequence instead of duplicating it.
"""

from __future__ import annotations

from app.events import queue as event_queue
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger


async def cleanup_conversation_dependents(
    conversation_storage: ConversationStorage, conversation_id: str
) -> None:
    """Before deleting a conversation, tear down anything homed on it.

    Deleting a conversation CASCADE-deletes any event subscriptions bound to it (a
    DB-level path that bypasses app cleanup) — which would orphan those rules' run
    rows and hidden run conversations. So first cascade-delete each bound rule's runs
    and disarm its manager, then discard this conversation's own queue/stream state
    and remove its Plan-mode files. Every step is best-effort (logged, never raised)
    so one failure can't strand the rest of a bulk clean.
    """
    from app.events.run_lifecycle import (
        delete_runs_for_subscription, SKILL, FILE_WATCHER, SCHEDULE,
    )
    from app.storage import (
        get_event_subscription_storage, get_file_watcher_storage,
        get_schedule_event_storage,
    )
    try:
        for sub in get_event_subscription_storage().list_by_conversation(conversation_id):
            await delete_runs_for_subscription(SKILL, sub["id"], sub.get("profile"))
    except Exception:  # noqa: BLE001
        logger.exception("clean conversation: skill-event run cascade failed")
    try:
        fw_mgr = None
        try:
            from app.events.file_watcher_manager import get_file_watcher_manager
            fw_mgr = get_file_watcher_manager()
        except Exception:  # noqa: BLE001
            fw_mgr = None
        for sub in get_file_watcher_storage().list_by_conversation(conversation_id):
            await delete_runs_for_subscription(FILE_WATCHER, sub["id"], sub.get("profile"))
            if fw_mgr is not None:
                try:
                    fw_mgr.disarm(sub)
                except Exception:  # noqa: BLE001
                    logger.debug("disarm failed during conv clean", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.exception("clean conversation: file-watcher run cascade failed")
    try:
        from app.events.schedule_manager import get_schedule_manager
        sch_mgr = get_schedule_manager()
        for sub in get_schedule_event_storage().list_by_conversation(conversation_id):
            await delete_runs_for_subscription(SCHEDULE, sub["id"], sub.get("profile"))
            try:
                sch_mgr.remove(sub["id"])
            except Exception:  # noqa: BLE001
                logger.debug("schedule remove failed during conv clean", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.exception("clean conversation: schedule run cascade failed")
    # Discard this conversation's own queue + stream-bus state.
    try:
        event_queue.discard_queue(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("discard_queue failed during conv clean", exc_info=True)
    try:
        from app.events.stream_bus import get_event_stream_bus
        await get_event_stream_bus().discard(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("bus.discard failed during conv clean", exc_info=True)
    # Remove any saved Plan-mode files for this conversation (best-effort).
    try:
        from app.utils.plans_dir import remove_conversation_plans
        conv = await conversation_storage.get_conversation(conversation_id)
        plan_profile = (conv or {}).get("profile")
        if plan_profile:
            remove_conversation_plans(plan_profile, conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("plans dir cleanup failed during conv clean", exc_info=True)


async def delete_all_chat(
    conversation_storage: ConversationStorage, profile: str
) -> int:
    """Cascade dependents for every chat conversation, then bulk-delete them.

    Mirrors ``handle_delete_all_conversations`` in the conversations API but without
    the API's change-event publishing (the caller decides when to publish). Returns
    the number of chat conversations deleted. ``delete_all_conversations`` is scoped
    to ``kind='chat'``, so hidden event-run conversations are handled by the per-rule
    cascade above, not swept blindly.
    """
    try:
        for conv in await conversation_storage.list_conversations(profile, limit=10_000):
            await cleanup_conversation_dependents(conversation_storage, conv["id"])
    except Exception:  # noqa: BLE001
        logger.exception("clean: conversation dependent cleanup failed")
    return await conversation_storage.delete_all_conversations(profile)
