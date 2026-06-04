"""Run the reasoning agent for one skill event with no chat history.

This is a thin adapter over :func:`app.agent.stream_runner.run_agent_to_bus`.
The skill-event path used to maintain its own copy of the agent loop and
persistence logic; that work has been folded into ``stream_runner`` so the
user-typed-message path and the skill-event path share one implementation
and one streaming protocol (SSE off the conversation stream bus).

The runner is set up by the server at boot via :func:`set_globals` so the
event manager and queue can call :func:`run_event` without holding direct
references to the agent and storage objects.
"""

from __future__ import annotations

from typing import Any

from app.utils.logger import logger


_cremind_agent: Any = None
_conversation_storage: Any = None


def set_globals(*, cremind_agent: Any, conversation_storage: Any) -> None:
    """Wire the runner to its collaborators (called once at server boot)."""
    global _cremind_agent, _conversation_storage
    _cremind_agent = cremind_agent
    _conversation_storage = conversation_storage


def get_cremind_agent() -> Any:
    return _cremind_agent


def get_conversation_storage() -> Any:
    return _conversation_storage


async def run_event(
    *,
    conversation_id: str,
    profile: str,
    skill_name: str,
    event_type: str,
    action: str,
    file_content: str,
) -> None:
    """Persist a synthetic user message, run the reasoning agent, persist the reply.

    Delegates to :func:`stream_runner.run_agent_to_bus`, which handles
    publishing every chunk to the conversation stream bus, persisting the
    assistant message, and emitting the terminal ``complete`` event.
    """
    if _cremind_agent is None or _conversation_storage is None:
        logger.error("Event runner globals not initialized; dropping event")
        return

    # Lazy import to break the circular dependency cycle:
    # events.__init__ → manager → queue → runner → stream_runner →
    # events.notifications_buffer → events.__init__.
    from app.agent.stream_runner import make_run_id, run_agent_to_bus

    logger.info(
        f"[skill_event] dispatching: conv={conversation_id} profile={profile} "
        f"skill={skill_name} event={event_type}"
    )

    # If this conversation is bound to an external channel, spawn a reply
    # forwarder so the agent's response also reaches the platform
    # (WhatsApp/Telegram/etc.). Without this, the run only goes to the web
    # UI's stream bus subscribers — the channel adapter wouldn't see it.
    # Must happen BEFORE run_agent_to_bus so the forwarder subscribes to the
    # bus before the run completes (the bus's replay buffer covers any
    # micro-gap between subscribe and the first publish).
    try:
        conv = await _conversation_storage.get_conversation(conversation_id)
        channel_id = (conv or {}).get("channel_id")
        if not channel_id:
            logger.debug(
                f"[skill_event] conv={conversation_id} has no channel_id; "
                f"skipping channel forwarder"
            )
        else:
            channel = await _conversation_storage.get_channel(channel_id)
            channel_type = (channel or {}).get("channel_type")
            logger.info(
                f"[skill_event] conv={conversation_id} channel_id={channel_id} "
                f"channel_type={channel_type}"
            )
            if channel and channel_type and channel_type != "main":
                from app.channels.registry import get_channel_registry
                adapter = get_channel_registry().get_adapter(channel_id)
                if adapter is None:
                    logger.warning(
                        f"[skill_event] no live adapter for channel_id={channel_id} "
                        f"type={channel_type} — agent reply will NOT reach platform"
                    )
                else:
                    await adapter.forward_external_run(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("[skill_event] channel forwarder setup failed")

    query = f"{action.strip()}\n\n{file_content.strip()}"
    metadata = {
        "source": "skill_event",
        "skill_name": skill_name,
        "event_type": event_type,
    }

    await run_agent_to_bus(
        cremind_agent=_cremind_agent,
        conversation_storage=_conversation_storage,
        conversation_id=conversation_id,
        run_id=make_run_id(conversation_id, kind="event"),
        profile=profile,
        query=query,
        history_messages=[],
        reasoning=True,
        user_message_metadata=metadata,
        agent_message_metadata=metadata,
        # Persist the trigger as a structured agent bubble (not a fake user
        # turn). The agent loop still receives ``query`` as user input.
        push_user_message=False,
        trigger_event={
            "event_type": event_type,
            "action": action.strip(),
            "content": file_content.strip(),
        },
        publish_notification=True,
        # Skill events run on existing conversations whose titles are
        # already meaningful; never rewrite them from the synthetic query.
        update_title_from_query=False,
    )
