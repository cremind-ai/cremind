"""Run the reasoning agent for one file-watcher event with no chat history.

Mirrors :mod:`app.events.runner` (skill events) but builds the synthetic
trigger payload from the watchdog event itself, not from a ``.md`` file on
disk. Both runners delegate to :func:`app.agent.stream_runner.run_agent_to_bus`
so user-typed-message and event-driven runs share one streaming protocol.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.utils.logger import logger


def _format_content(payload: Dict[str, Any]) -> str:
    """Build the content block describing the watchdog event (event_type,
    target_kind, path, watch_name, extension, detected_at, plus src/dest on
    ``moved`` events)."""
    lines = [
        f"event_type: {payload.get('event_type', '')}",
        f"target_kind: {payload.get('target_kind', '')}",
    ]
    src_path = payload.get("src_path")
    dest_path = payload.get("dest_path")
    if payload.get("event_type") == "moved" and src_path and dest_path:
        lines.append(f"src_path: {src_path}")
        lines.append(f"dest_path: {dest_path}")
    else:
        lines.append(f"path: {payload.get('path', '')}")
    lines.extend([
        f"watch_name: {payload.get('watch_name', '')}",
        f"extension: {payload.get('extension', '')}",
        f"detected_at: {payload.get('detected_at', '')}",
    ])
    return "\n".join(lines)


def build_trigger_messages(action: str, payload: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(reasoning_query, bubble_content)`` for a file-watcher event.

    The reasoning ``Input:`` is the action command followed by the event
    details (so the agent can act on what changed). The UI bubble keeps the
    fenced detail block (rendered by stream_runner), so it is unchanged.
    """
    block = _format_content(payload)
    query = f"{action.strip()}\n\n{block}".strip()
    return query, f"---\n{block}\n---"


async def run_event(
    *,
    conversation_id: str,
    profile: str,
    subscription_id: str,
    watch_name: str,
    action: str,
    payload: Dict[str, Any],
) -> None:
    """Synthesize a trigger message + invoke the reasoning agent.

    ``payload`` is the watchdog event description produced by
    :class:`FileWatcherManager` (event_type, target_kind, path, extension,
    detected_at, optionally src_path/dest_path).
    """
    # Late imports avoid circular import at package load:
    # events.__init__ → file_watcher_manager → queue → file_watcher_runner →
    # stream_runner → events.notifications_buffer → events.__init__.
    from app.events import runner as skill_event_runner
    from app.agent.stream_runner import make_run_id, run_agent_to_bus

    cremind_agent = skill_event_runner.get_cremind_agent()
    conversation_storage = skill_event_runner.get_conversation_storage()
    if cremind_agent is None or conversation_storage is None:
        logger.error(
            "[file_watcher_event] runner globals not initialized; dropping event"
        )
        return

    logger.info(
        f"[file_watcher_event] dispatching: conv={conversation_id} "
        f"profile={profile} watch={watch_name} "
        f"event={payload.get('event_type')} path={payload.get('path')}"
    )

    # Channel forwarder: same idea as skill events. If this conversation is
    # bound to an external messaging channel, subscribe a forwarder so the
    # agent's reply also reaches the platform (WhatsApp/Telegram/etc.).
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
                        f"[file_watcher_event] no live adapter for "
                        f"channel_id={channel_id} type={channel_type}"
                    )
                else:
                    await adapter.forward_external_run(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("[file_watcher_event] channel forwarder setup failed")

    # query = action command + event details (reasoning Input); bubble_content =
    # the fenced detail block (UI bubble, unchanged).
    query, bubble_content = build_trigger_messages(action, payload)
    metadata: Dict[str, Any] = {
        "source": "file_watcher_event",
        "subscription_id": subscription_id,
        "watch_name": watch_name,
        "event_type": payload.get("event_type"),
        "path": payload.get("path"),
    }
    if payload.get("src_path"):
        metadata["src_path"] = payload["src_path"]
    if payload.get("dest_path"):
        metadata["dest_path"] = payload["dest_path"]

    trigger_event: Dict[str, Any] = {
        "event_type": payload.get("event_type"),
        "action": action.strip(),
        "content": bubble_content,
        "watch_name": watch_name,
        "target_kind": payload.get("target_kind"),
        "path": payload.get("path"),
        "extension": payload.get("extension"),
        "detected_at": payload.get("detected_at"),
    }
    if payload.get("src_path"):
        trigger_event["src_path"] = payload["src_path"]
    if payload.get("dest_path"):
        trigger_event["dest_path"] = payload["dest_path"]

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
