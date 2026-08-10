"""Returning an EVENT TASK's result to the conversation that was waiting for it.

Ordinary event runs are fire-and-forget: they execute in a hidden conversation
and surface as a notification. An *event task* is the other shape — the agent
registered it mid-flow precisely because it cannot finish without the outcome
("open the PR, wait for CI, then merge"). So when a task run reaches a terminal
status, its final answer is injected back into the origin conversation as a new
turn, the flow continues there, and the one-shot subscription terminates.

Three properties this module is responsible for:

**Exactly once.** :meth:`EventRunStorage.claim_delivery` is a conditional UPDATE
on ``origin_delivered_at IS NULL``. It is claimed *before* the injection, so the
failure mode is a lost delivery (recoverable, and the window is one ``await``)
rather than a duplicated agent turn in the user's chat. Three racers are covered:
the live terminal hook, the boot sweep, and a run that goes terminal twice
because someone replied inside a finished run's mini-chat.

**Every terminal path.** The hook lives in ``stream_runner``'s terminal
finalize rather than in the dispatcher, so it also fires for a task run that
parked as ``pending`` (``request_user_input``), was answered by a human hours
later, and only then completed — that terminal comes from a different call
stack entirely.

**Never a silent hang.** A failed run, a run interrupted by a restart, a task
whose event never fired before its deadline: each delivers *something* back, so
a conversation is never left waiting on a result that will not arrive.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.utils.logger import logger

# Return values of :func:`on_run_terminal`, so callers can branch without
# re-reading the DB (the stream runner uses them to suppress the run's own
# notification when the origin conversation is about to speak instead).
DELIVERED = "delivered"
NOT_TASK = "not_task"
ALREADY_DELIVERED = "already_delivered"
ORIGIN_GONE = "origin_gone"
SKIPPED_CANCELLED = "skipped_cancelled"
FAILED = "failed"

#: Outcomes where the origin conversation itself raises the user-facing
#: notification, so the hidden run must not raise a second one.
SUPPRESSES_RUN_NOTIFICATION = (DELIVERED, SKIPPED_CANCELLED)

_NO_OUTPUT = "(the task run produced no output)"


# ── message construction ────────────────────────────────────────────────────


def build_task_result_messages(
    run: Dict[str, Any],
    *,
    status: str,
    result_text: str,
    timed_out: bool = False,
) -> tuple[str, Dict[str, Any]]:
    """Build ``(query, trigger_event)`` for the continuation turn.

    Pure, so the wording is unit-testable. ``query`` is what the model reads;
    ``trigger_event`` renders the structured bubble the user sees (and marks the
    turn as event-triggered, which is what lets the agent chain one more task
    but not a standing automation).
    """
    label = run.get("label") or "event task"
    action = run.get("action") or ""
    depth = int((run.get("trigger_payload") or {}).get("task_chain_depth") or 0)

    if timed_out:
        status_word = "timed out"
        body = (
            f"Result:\n{result_text}\n\n"
            "Tell the user it timed out and what you do know; do NOT assume the "
            "outcome happened. If it is worth waiting longer, register a new "
            "one-shot task with a larger timeout_minutes."
        )
    elif status == "completed":
        status_word = "completed"
        body = (
            f"Result:\n{result_text}\n\n"
            "Continue the original flow from here: carry out the step that was "
            "waiting on this outcome. If the flow needs one more wait, register "
            "the NEXT one-shot task and end your turn. If it is finished, give "
            "the user the final answer. Do not re-register the task that just "
            "finished, and do not repeat work already done above."
        )
    else:
        status_word = "failed"
        body = (
            f"Failure:\n{result_text}\n\n"
            "Report the failure to the user and propose a next step. Do not "
            "silently retry more than once."
        )

    query = (
        "[Event task result] The one-shot task registered earlier in this "
        "conversation has finished. This turn continues the flow that was "
        "waiting on it; the full conversation history is above.\n"
        f"Awaited: {label}\n"
        f"Task action: {action}\n"
        f"Status: {status_word}\n\n"
        f"{body}"
    )
    trigger_event = {
        "kind": "event_task_result",
        "event_type": f"event task {status_word}: {label}",
        "action": action,
        "content": result_text,
        "status": status_word,
        "source_kind": run.get("source_kind"),
        "subscription_id": run.get("subscription_id"),
        "event_run_id": run.get("id"),
        "task_chain_depth": depth + 1,
    }
    return query, trigger_event


# ── delivery ────────────────────────────────────────────────────────────────


async def on_run_terminal(
    *,
    event_run_id: str,
    profile: str,
    status: str,
    final_text: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    """Deliver a finished task run's result into its origin conversation.

    Safe to call for every terminal event run — a non-task run returns
    :data:`NOT_TASK` after one cheap read. Never raises: a delivery problem must
    not turn a completed run into a failed one.
    """
    from app.storage import get_event_run_storage

    store = get_event_run_storage()
    try:
        run = await store.get(event_run_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] failed to load run {event_run_id}")
        return FAILED
    if not run or not run.get("deliver_to_origin"):
        return NOT_TASK

    if not await store.claim_delivery(event_run_id):
        # Someone else already delivered this result (or is about to).
        return ALREADY_DELIVERED

    source_kind = run.get("source_kind") or ""
    subscription_id = run.get("subscription_id") or ""

    # A cancelled run is a deliberate kill from the Events page. Injecting "your
    # task was cancelled" into the chat would be noise on top of an action the
    # user just took there, so terminate the rule quietly instead.
    if status == "cancelled":
        _terminate_task_subscription(source_kind, subscription_id, "cancelled")
        return SKIPPED_CANCELLED

    timed_out = bool((run.get("trigger_payload") or {}).get("timed_out"))
    result_text = (
        (final_text or "").strip()
        or (error or "").strip()
        or (run.get("error") or "").strip()
        or (run.get("pending_question") or "").strip()
        or _NO_OUTPUT
    )

    origin_id = run.get("origin_conversation_id")
    if not origin_id:
        # The origin conversation was deleted; its subscriptions went with it
        # (FK cascade). Nothing to continue — leave the run's own notification
        # in place so the outcome is not lost entirely.
        _terminate_task_subscription(source_kind, subscription_id, "completed")
        return ORIGIN_GONE

    try:
        outcome = await _inject_into_origin(
            run=run, profile=profile, origin_id=origin_id,
            status=status, result_text=result_text, timed_out=timed_out,
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] delivery failed for run {event_run_id}")
        outcome = FAILED

    if outcome == ORIGIN_GONE:
        # Nothing to continue and nothing to retry — keep the claim so the boot
        # sweep does not pick this row up forever (which would also pin it
        # against the run-history prune).
        _terminate_task_subscription(source_kind, subscription_id, "completed")
        return ORIGIN_GONE
    if outcome != DELIVERED:
        # Transient: release the claim so the next boot sweep tries again.
        try:
            await store.clear_delivery_claim(event_run_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"[event_task] failed to release claim on {event_run_id}")
        return FAILED

    _terminate_task_subscription(source_kind, subscription_id, "completed")
    _publish_changed(profile, source_kind)
    logger.info(
        f"[event_task] delivered {source_kind} task result "
        f"(run={event_run_id}, status={status}) to conversation {origin_id}"
    )
    return DELIVERED


async def _inject_into_origin(
    *,
    run: Dict[str, Any],
    profile: str,
    origin_id: str,
    status: str,
    result_text: str,
    timed_out: bool,
) -> str:
    """Queue the continuation turn on the origin conversation.

    Returns :data:`DELIVERED`, :data:`ORIGIN_GONE` (permanent — do not retry) or
    :data:`FAILED` (transient — the caller releases its claim so a later sweep
    retries).
    """
    from app.events import queue as event_queue
    from app.events import runner as event_runner

    conversation_storage = event_runner.get_conversation_storage()
    if conversation_storage is None:
        logger.error("[event_task] conversation storage not initialized; cannot deliver")
        return FAILED

    conv = await conversation_storage.get_conversation(origin_id)
    if conv is None:
        logger.warning(f"[event_task] origin conversation {origin_id} is gone")
        return ORIGIN_GONE
    if conv.get("kind") == "event_run":
        # Defensive: event runs cannot register tasks, so this should be
        # unreachable. Delivering into one would nest hidden runs.
        logger.warning(
            f"[event_task] origin {origin_id} is itself an event run; skipping delivery"
        )
        return ORIGIN_GONE

    history_messages = await _load_history(conversation_storage, origin_id, profile)

    # Mirror the continuation to the platform when the origin chat lives on an
    # external channel. The task run's own output was deliberately NOT forwarded
    # (see run_dispatcher), so the user gets exactly one message: this one.
    try:
        from app.events.run_dispatcher import _maybe_forward_to_channel
        await _maybe_forward_to_channel(conversation_storage, origin_id, origin_id)
    except Exception:  # noqa: BLE001
        logger.exception("[event_task] channel forwarder setup failed")

    query, trigger_event = build_task_result_messages(
        run, status=status, result_text=result_text, timed_out=timed_out,
    )
    metadata = {
        "source": "event_task_result",
        "status": trigger_event["status"],
        "source_kind": run.get("source_kind"),
        "subscription_id": run.get("subscription_id"),
        "event_run_id": run.get("id"),
    }

    from app.agent.stream_runner import make_run_id

    await event_queue.enqueue_user_message(
        conversation_id=origin_id,
        run_id=make_run_id(origin_id, kind="event"),
        profile=profile,
        query=query,
        history_messages=history_messages,
        reasoning=True,
        user_message_metadata=metadata,
        agent_message_metadata=metadata,
        push_user_message=False,
        trigger_event=trigger_event,
        update_title_from_query=False,
        publish_notification=True,
    )
    return DELIVERED


async def _load_history(conversation_storage: Any, conversation_id: str, profile: str) -> list:
    """Load the origin conversation's history, exactly like a web POST does."""
    try:
        from app.config.user_config import replay_reasoning_enabled
        from app.utils.common import convert_db_messages_to_history

        db_msgs = await conversation_storage.get_messages(conversation_id)
        if db_msgs:
            return convert_db_messages_to_history(
                db_msgs, include_reasoning=replay_reasoning_enabled(profile),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_task] failed to load history for {conversation_id}; "
            "continuing with an empty history"
        )
    return []


# ── timeout ─────────────────────────────────────────────────────────────────


async def deliver_timeout(source_kind: str, sub: Dict[str, Any]) -> str:
    """Report an expired task back to its origin conversation.

    The caller has already claimed the subscription (``active -> timed_out``),
    so this runs at most once per task. A terminal ``event_runs`` row is written
    with no conversation of its own: the timeout produced no agent run, but the
    row makes the outcome visible on the Events page and — more importantly —
    lets the delivery ride the same crash-safe claim/sweep path as a real run.
    """
    from app.storage import get_event_run_storage

    profile = sub.get("profile") or ""
    origin_id = sub.get("conversation_id")
    label = _subscription_label(source_kind, sub)
    deadline = _format_epoch(sub.get("timeout_at"), profile)
    message = (
        f"The awaited event never fired before the deadline ({deadline}), so "
        "the task action never ran."
    )
    try:
        created = await get_event_run_storage().create(
            profile=profile,
            source_kind=source_kind,
            subscription_id=sub["id"],
            conversation_id=None,
            label=label,
            action=sub.get("action") or "",
            trigger_payload={"timed_out": True, "timeout_at": sub.get("timeout_at")},
            origin_conversation_id=origin_id,
            deliver_to_origin=bool(origin_id),
            status="failed",
            error=message,
            finished=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] failed to record timeout run for {sub.get('id')}")
        return FAILED

    _publish_changed(profile, source_kind)
    return await on_run_terminal(
        event_run_id=created["run"]["id"],
        profile=profile,
        status="failed",
        final_text=message,
        error=message,
    )


# ── boot recovery ───────────────────────────────────────────────────────────


async def sweep_undelivered() -> int:
    """Deliver task results a crash left stranded. Idempotent; runs at boot.

    Must run AFTER :meth:`EventRunStorage.recover_after_restart`, which flips
    interrupted ``running`` rows to ``failed`` — that is what makes them
    terminal, and therefore deliverable, here.
    """
    from app.storage import get_event_run_storage

    store = get_event_run_storage()
    try:
        rows = await store.list_undelivered_task_runs()
    except Exception:  # noqa: BLE001
        logger.exception("[event_task] boot sweep: failed to list undelivered runs")
        return 0

    delivered = 0
    for run in rows:
        try:
            final_text = await _recover_final_text(run)
            outcome = await on_run_terminal(
                event_run_id=run["id"],
                profile=run.get("profile") or "",
                status=run.get("status") or "failed",
                final_text=final_text,
                error=run.get("error"),
            )
            if outcome == DELIVERED:
                delivered += 1
        except Exception:  # noqa: BLE001
            logger.exception(f"[event_task] boot sweep: delivery failed for {run.get('id')}")
    if delivered:
        logger.info(f"[event_task] boot sweep delivered {delivered} stranded task result(s)")

    await _reconcile_orphaned_tasks()
    return delivered


async def _recover_final_text(run: Dict[str, Any]) -> Optional[str]:
    """Recover a completed run's answer from its conversation (nothing in memory)."""
    if run.get("status") != "completed" or not run.get("conversation_id"):
        return run.get("error")
    try:
        from app.events import runner as event_runner

        conversation_storage = event_runner.get_conversation_storage()
        if conversation_storage is None:
            return run.get("error")
        msgs = await conversation_storage.get_messages(run["conversation_id"])
        for msg in reversed(msgs or []):
            if msg.get("role") == "agent" and (msg.get("content") or "").strip():
                return msg["content"]
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] could not recover final text for run {run.get('id')}")
    return run.get("error")


async def _reconcile_orphaned_tasks() -> None:
    """Re-arm or close tasks stuck mid-claim by a crash.

    A task claimed at fire time but crashed before its run row existed would sit
    at ``triggered`` forever with nothing to deliver. With no live and no
    undeliverable run behind it, the honest state is "spent".
    """
    from app.storage import (
        get_event_run_storage, get_event_subscription_storage,
        get_file_watcher_storage,
    )

    run_store = get_event_run_storage()
    for source_kind, storage in (
        ("skill_event", get_event_subscription_storage()),
        ("file_watcher", get_file_watcher_storage()),
    ):
        try:
            rows = [r for r in storage.list_all() if r.get("task_status") == "triggered"]
        except Exception:  # noqa: BLE001
            logger.exception(f"[event_task] reconcile: failed to list {source_kind} tasks")
            continue
        for sub in rows:
            try:
                if await run_store.has_live_run_for_subscription(source_kind, sub["id"]):
                    continue
                storage.set_task_status(sub["id"], "completed")
                logger.info(
                    f"[event_task] reconciled orphaned {source_kind} task {sub['id']} "
                    "(claimed but no run survived) → completed"
                )
            except Exception:  # noqa: BLE001
                logger.exception(f"[event_task] reconcile failed for {sub.get('id')}")


# ── small helpers ───────────────────────────────────────────────────────────


def _task_storage(source_kind: str) -> Any:
    from app.storage import get_event_subscription_storage, get_file_watcher_storage

    if source_kind == "skill_event":
        return get_event_subscription_storage()
    if source_kind == "file_watcher":
        return get_file_watcher_storage()
    return None  # schedules terminate inside ScheduleManager._fire


def _terminate_task_subscription(source_kind: str, subscription_id: str, status: str) -> None:
    storage = _task_storage(source_kind)
    if storage is None or not subscription_id:
        return
    try:
        storage.set_task_status(subscription_id, status)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_task] failed to mark {source_kind} task {subscription_id} {status}"
        )


def _subscription_label(source_kind: str, sub: Dict[str, Any]) -> str:
    if source_kind == "skill_event":
        skill = sub.get("skill_name") or ""
        event_type = sub.get("event_type") or ""
        return f"{skill}:{event_type}" if skill else event_type
    return sub.get("name") or "File watcher"


def _format_epoch(epoch: Any, profile: str) -> str:
    if not epoch:
        return "unknown"
    try:
        from datetime import datetime
        from app.config.timezone import resolve_tzinfo

        return datetime.fromtimestamp(float(epoch), resolve_tzinfo(profile)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def _publish_changed(profile: str, source_kind: str) -> None:
    """Nudge the Events page: the run list and the rule's own section changed."""
    try:
        from app.events.event_runs_admin_bus import publish_event_runs_changed
        publish_event_runs_changed(profile)
    except Exception:  # noqa: BLE001
        logger.debug("[event_task] event-runs admin nudge failed", exc_info=True)
    # Published straight on the bus (not via the app.api helpers) so this module
    # stays free of an app.events → app.api import edge. Payload-less by design:
    # subscribers rebuild their own snapshot on the tick.
    try:
        if source_kind == "skill_event":
            from app.events.skill_events_admin_bus import get_skill_events_admin_stream_bus
            get_skill_events_admin_stream_bus().publish(profile, {})
        elif source_kind == "file_watcher":
            from app.events.file_watcher_admin_bus import get_file_watcher_admin_stream_bus
            get_file_watcher_admin_stream_bus().publish(profile, {})
    except Exception:  # noqa: BLE001
        logger.debug("[event_task] rule admin nudge failed", exc_info=True)
