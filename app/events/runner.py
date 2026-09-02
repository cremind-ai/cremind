"""Boot-wired globals for the event pipeline, plus gate-usage accounting.

The server calls :func:`set_globals` at boot so the run dispatcher, the delivery
layer, the per-conversation queue and the group fan-out can reach the agent and
the conversation storage without holding direct references to them.

Event runs themselves are executed by :mod:`app.events.run_dispatcher`, which
runs each fired trigger in its own hidden conversation through
:func:`app.agent.stream_runner.run_agent_to_bus`.
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
    conversation_id: str | None,
    profile: str,
    event_type: str,
    message_id: str | None,
    event_run_id: str | None = None,
) -> None:
    """Persist the matching gate's LLM call as an ``event_gate`` usage record.

    Best-effort: usage accounting must never break event delivery. The gate runs
    a cheap model, but its cost is still attributed (per the product requirement)
    as a request type distinct from reasoning/tool calls. ``conversation_id`` may
    be ``None`` (a rejected event never opens a conversation); ``event_run_id``
    attributes matched-gate cost to the run it precedes.
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
            event_run_id=event_run_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[skill_event] failed to record event_gate usage")

