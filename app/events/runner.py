"""Run the reasoning agent for one skill event on its conversation.

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


async def _record_gate_usage(
    *,
    llm: Any,
    tokens: dict,
    conversation_id: str,
    profile: str,
    event_type: str,
    message_id: str | None,
) -> None:
    """Persist the matching gate's LLM call as an ``event_gate`` usage record.

    Best-effort: usage accounting must never break event delivery. The gate runs
    a cheap model, but its cost is still attributed (per the product requirement)
    as a request type distinct from reasoning/tool calls.
    """
    if llm is None or not tokens or not any(tokens.values()):
        return
    try:
        from app.agent.usage import UsageRecord
        from app.storage import get_usage_storage

        record = UsageRecord(
            source_kind="event_gate",
            tool_id=None,
            label=f"Event filter: {event_type}",
            provider=getattr(llm, "provider_name", None),
            model=getattr(llm, "model_name", None),
            model_group=None,
            step_index=0,
            input_tokens=int(tokens.get("input_tokens") or 0),
            cache_read_input_tokens=int(tokens.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(tokens.get("cache_creation_input_tokens") or 0),
            output_tokens=int(tokens.get("output_tokens") or 0),
        )
        await get_usage_storage().add_usage_records(
            conversation_id=conversation_id,
            profile=profile,
            records=[record.to_dict()],
            message_id=message_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[skill_event] failed to record event_gate usage")


async def _announce_rejected_trigger(
    *,
    conversation_id: str,
    profile: str,
    skill_name: str,
    event_type: str,
    action: str,
    file_content: str,
    reason: str,
) -> str | None:
    """Persist a UI-only 'rejected trigger' bubble and announce it on the bus.

    The message carries ``metadata.ui_only=True`` so the Reasoning Agent never
    sees it (filtered in ``convert_db_messages_to_history``) — the user sees that
    an event arrived and was filtered out, but no agent turn runs. Returns the new
    message id (for usage attribution) or ``None`` on persistence failure.
    """
    # Lazy import: stream_runner pulls in events.* (circular at module load time).
    from app.agent.stream_runner import _format_trigger_content
    from app.events.stream_bus import get_event_stream_bus

    content = _format_trigger_content(
        event_type=event_type,
        action=action.strip(),
        content=file_content.strip(),
    )
    metadata = {
        "ui_only": True,
        "kind": "rejected_trigger",
        "rejected_reason": reason,
        "source": "skill_event",
        "skill_name": skill_name,
        "event_type": event_type,
    }

    bus = get_event_stream_bus()
    # start_run records the owning profile so publish() fans the event to the
    # profile-scoped UI stream; end_run clears the replay ring (the persisted
    # message is then served via the normal messages fetch).
    await bus.start_run(conversation_id, profile)
    msg_id: str | None = None
    try:
        msg = await _conversation_storage.add_message(
            conversation_id=conversation_id,
            role="agent",
            content=content,
            metadata=metadata,
        )
        msg_id = msg.get("id") if isinstance(msg, dict) else None
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[skill_event] failed to persist rejected trigger for {conversation_id}"
        )
    try:
        await bus.publish(conversation_id, "event_trigger_rejected", {
            "id": msg_id,
            "content": content,
            "metadata": metadata,
            "reason": reason,
        })
    finally:
        await bus.end_run(conversation_id)
    return msg_id


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

    # ── Matching gate ────────────────────────────────────────────────────
    # Before spending a full reasoning turn, run a cheap small-model classifier
    # to check the event content actually satisfies the subscription's action
    # (e.g. "…from li@olli-ai.com"). Always-on; fail-open — any error treats the
    # event as a match so a real event is never silently dropped.
    from app.events.gate import classify_event_match

    gate_llm: Any = None
    try:
        gate_llm = _cremind_agent.low_performance_llm(profile)
        gate = await classify_event_match(
            llm=gate_llm,
            event_type=event_type,
            action=action,
            file_content=file_content,
        )
        matched, reason, tokens = gate.matched, gate.reason, gate.tokens
    except Exception:  # noqa: BLE001
        logger.exception("[skill_event] gate failed; failing open (treating as match)")
        matched, reason, tokens = True, "gate error; defaulted to match", {}

    if not matched:
        logger.info(
            f"[skill_event] gate REJECTED: conv={conversation_id} event={event_type} "
            f"reason={reason!r} — skipping agent run"
        )
        rejected_msg_id = await _announce_rejected_trigger(
            conversation_id=conversation_id,
            profile=profile,
            skill_name=skill_name,
            event_type=event_type,
            action=action,
            file_content=file_content,
            reason=reason,
        )
        await _record_gate_usage(
            llm=gate_llm, tokens=tokens, conversation_id=conversation_id,
            profile=profile, event_type=event_type, message_id=rejected_msg_id,
        )
        return

    # Matched → account the gate's cost (conversation-scoped; the agent's own
    # messages don't exist yet) and proceed to the full reasoning run.
    await _record_gate_usage(
        llm=gate_llm, tokens=tokens, conversation_id=conversation_id,
        profile=profile, event_type=event_type, message_id=None,
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

    # Load this conversation's prior history so the event run reuses the SAME
    # [system + tools + history] prompt prefix as a normal chat turn (prompt-cache
    # reuse). With compaction enabled (the default) stream_runner rebuilds history
    # from storage and ignores this value; loading it here keeps the event run
    # correct even when compaction is disabled, instead of starting cold. The
    # current trigger message is persisted later inside run_agent_to_bus, so it is
    # not in this snapshot. Best-effort — a load failure must never block delivery.
    history_messages: list = []
    try:
        from app.config.user_config import replay_reasoning_enabled
        from app.utils.common import convert_db_messages_to_history

        db_msgs = await _conversation_storage.get_messages(conversation_id)
        if db_msgs:
            history_messages = convert_db_messages_to_history(
                db_msgs, include_reasoning=replay_reasoning_enabled(profile),
            )
    except Exception:  # noqa: BLE001
        logger.exception(f"[skill_event] failed to load history for {conversation_id}")

    await run_agent_to_bus(
        cremind_agent=_cremind_agent,
        conversation_storage=_conversation_storage,
        conversation_id=conversation_id,
        run_id=make_run_id(conversation_id, kind="event"),
        profile=profile,
        query=query,
        history_messages=history_messages,
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
