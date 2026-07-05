"""Shared teardown for event runs and their hidden conversations.

Two entry points used by delete flows:

- :func:`discard_run_conversation` — tear down one run conversation's in-memory
  queue + stream-bus state and delete the conversation row (messages CASCADE,
  usage rows SET-NULL their conversation_id so Usage & Cost keeps counting).
- :func:`delete_runs_for_subscription` — app-level cascade when an event rule is
  deleted: cancel any running runs, delete all their conversations, delete the
  run rows. Called before the subscription row itself is removed.

Kept separate from :mod:`app.events.run_dispatcher` (which imports from here) so
there's no import cycle.
"""

from __future__ import annotations

from typing import Optional

from app.utils.logger import logger

# subscription "kind" → event_runs.source_kind value.
SKILL = "skill_event"
FILE_WATCHER = "file_watcher"
SCHEDULE = "schedule"


async def discard_run_conversation(conversation_id: Optional[str]) -> None:
    """Tear down a run conversation's queue/stream state, then delete it."""
    if not conversation_id:
        return
    from app.events import queue as event_queue
    from app.events.stream_bus import get_event_stream_bus
    from app.storage import get_conversation_storage

    try:
        event_queue.discard_queue(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("[event_run] discard_queue failed", exc_info=True)
    try:
        await get_event_stream_bus().discard(conversation_id)
    except Exception:  # noqa: BLE001
        logger.debug("[event_run] bus.discard failed", exc_info=True)
    try:
        await get_conversation_storage().delete_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_run] failed to delete run conversation {conversation_id}")


async def delete_runs_for_subscription(
    source_kind: str, subscription_id: str, profile: Optional[str] = None,
) -> int:
    """Cascade-delete all runs of a rule + their hidden conversations.

    Usage rows survive (conversation_id SET-NULLs; event_run_id stays), so
    Usage & Cost keeps counting the deleted runs. Returns the number of runs
    removed. Safe to call for a rule with no runs.
    """
    from app.agent.stream_runner import cancel_run, is_running
    from app.storage import get_event_run_storage
    from app.events.event_runs_admin_bus import publish_event_runs_changed

    store = get_event_run_storage()
    try:
        runs = await store.list_for_subscription(source_kind, subscription_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_run] failed to list runs for {source_kind}:{subscription_id}"
        )
        return 0

    # Cancel any in-flight runs first so their tasks stop touching the DB/bus.
    for run in runs:
        rid = run.get("run_id")
        if run.get("status") == "running" and rid and is_running(rid):
            try:
                cancel_run(rid)
            except Exception:  # noqa: BLE001
                logger.debug("[event_run] cancel_run failed during cascade", exc_info=True)

    # Delete the run rows, then tear down their conversations.
    try:
        conv_ids = await store.delete_for_subscription(source_kind, subscription_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_run] failed to delete runs for {source_kind}:{subscription_id}"
        )
        return 0
    for conv_id in conv_ids:
        await discard_run_conversation(conv_id)

    if profile:
        try:
            publish_event_runs_changed(profile)
        except Exception:  # noqa: BLE001
            pass
    return len(runs)
