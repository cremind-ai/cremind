"""Run the reasoning agent for one schedule (time-based) event, no chat history.

Mirrors :mod:`app.events.file_watcher_runner` but the synthetic trigger payload
comes from a clock tick produced by :class:`app.events.schedule_manager.ScheduleManager`,
not a watchdog event. Both delegate to
:func:`app.agent.stream_runner.run_agent_to_bus` so user-typed-message and
event-driven runs share one streaming protocol.

Only *action* schedule events reach this runner. Reminder-only events are
handled inline by the manager (a push notification), never an agent run.
"""

from __future__ import annotations

from typing import Any, Dict

from app.utils.logger import logger


def _format_content(payload: Dict[str, Any]) -> str:
    """Build the trigger's content block (the detail lines shown in the UI bubble)."""
    lines = [
        f"title: {payload.get('title', '')}",
        f"fired_at: {payload.get('fired_at', '')}",
        f"schedule_kind: {payload.get('schedule_kind', '')}",
    ]
    if payload.get("rrule"):
        lines.append(f"rrule: {payload['rrule']}")
    if payload.get("next_fire_at_iso"):
        lines.append(f"next_occurrence: {payload['next_fire_at_iso']}")
    return "\n".join(lines)


def build_trigger_messages(action: str, payload: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(reasoning_query, bubble_content)`` for a fired schedule event.

    The reasoning ``Input:`` is just the action (the user's command, e.g.
    "tắt đèn hiên") — the schedule metadata is noise for execution. The UI
    bubble keeps the fenced detail block (rendered by stream_runner's
    ``_format_trigger_content``), so it is unchanged.
    """
    block = _format_content(payload)
    return action.strip(), f"---\n{block}\n---"


async def run_event(
    *,
    conversation_id: str,
    profile: str,
    subscription_id: str,
    title: str,
    action: str,
    payload: Dict[str, Any],
) -> None:
    """Synthesize a trigger message + invoke the reasoning agent in the bound
    conversation (the one that registered the schedule, or the per-profile
    ``__schedule__`` conversation for manual calendar events)."""
    # Late imports avoid circular import at package load (mirrors
    # file_watcher_runner): events.__init__ -> schedule_manager -> queue ->
    # schedule_event_runner -> stream_runner -> events.notifications_buffer.
    from app.events import runner as skill_event_runner
    from app.agent.stream_runner import make_run_id, run_agent_to_bus

    cremind_agent = skill_event_runner.get_cremind_agent()
    conversation_storage = skill_event_runner.get_conversation_storage()
    if cremind_agent is None or conversation_storage is None:
        logger.error(
            "[schedule_event] runner globals not initialized; dropping event"
        )
        return

    logger.info(
        f"[schedule_event] dispatching: conv={conversation_id} profile={profile} "
        f"sub={subscription_id} title={title!r} fired_at={payload.get('fired_at')}"
    )

    # Channel forwarder: if this conversation is bound to an external messaging
    # channel, subscribe a forwarder so the agent's reply also reaches the
    # platform (same idea as skill / file-watcher events).
    try:
        conv = await conversation_storage.get_conversation(conversation_id)
        channel_id = (conv or {}).get("channel_id")
        if channel_id:
            channel = await conversation_storage.get_channel(channel_id)
            channel_type = (channel or {}).get("channel_type")
            if channel and channel_type and channel_type != "main":
                from app.channels.registry import get_channel_registry
                adapter = get_channel_registry().get_adapter(channel_id)
                if adapter is None:
                    logger.warning(
                        f"[schedule_event] no live adapter for "
                        f"channel_id={channel_id} type={channel_type}"
                    )
                else:
                    await adapter.forward_external_run(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("[schedule_event] channel forwarder setup failed")

    # query = the bare command (reasoning Input); bubble_content = the fenced
    # detail block (UI bubble, unchanged).
    query, bubble_content = build_trigger_messages(action, payload)
    metadata: Dict[str, Any] = {
        "source": "schedule_event",
        "subscription_id": subscription_id,
        "title": title,
        "schedule_kind": payload.get("schedule_kind"),
        "fired_at": payload.get("fired_at"),
    }
    trigger_event: Dict[str, Any] = {
        "event_type": "schedule",
        "action": action.strip(),
        "content": bubble_content,
        "title": title,
        "schedule_kind": payload.get("schedule_kind"),
        "rrule": payload.get("rrule"),
        "fired_at": payload.get("fired_at"),
        "next_occurrence": payload.get("next_fire_at_iso"),
    }

    await run_agent_to_bus(
        cremind_agent=cremind_agent,
        conversation_storage=conversation_storage,
        conversation_id=conversation_id,
        run_id=make_run_id(conversation_id, kind="event"),
        profile=profile,
        query=query,
        history_messages=[],
        reasoning=True,
        user_message_metadata=metadata,
        agent_message_metadata=metadata,
        push_user_message=False,
        trigger_event=trigger_event,
        publish_notification=True,
        update_title_from_query=False,
    )
