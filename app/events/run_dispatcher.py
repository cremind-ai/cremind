"""Parallel dispatch of fired event triggers into isolated per-run conversations.

Replaces the old "enqueue onto the registering conversation's FIFO" model. Each
fired trigger (skill / file-watcher / schedule) now:

1. queues on a **per-rule** FIFO (key ``f"{source_kind}:{subscription_id}"``) so
   two firings of the *same* rule serialize, but different rules run in parallel;
2. acquires a slot on a **process-global semaphore** (``max_parallel_runs``) so
   a burst can't spawn unbounded concurrent LLM runs;
3. runs in a **fresh hidden conversation** (``kind='event_run'``) tracked by one
   ``event_runs`` row — never the registering conversation, never sharing chat
   history.

User messages keep their own per-conversation queue in :mod:`app.events.queue`.

The skill-event matching gate runs here (pre-run): a rejected event records
``event_gate`` usage with no conversation/run and never creates a run row.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from app.utils.logger import logger

# key f"{source_kind}:{subscription_id}" → per-rule FIFO / worker
_sub_queues: Dict[str, asyncio.Queue] = {}
_sub_workers: Dict[str, asyncio.Task] = {}
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        from app.events.run_config import max_parallel_runs
        _semaphore = asyncio.Semaphore(max_parallel_runs())
    return _semaphore


def _key(source_kind: str, subscription_id: str) -> str:
    return f"{source_kind}:{subscription_id}"


def _ensure_worker(key: str) -> asyncio.Queue:
    queue = _sub_queues.get(key)
    if queue is None:
        queue = asyncio.Queue()
        _sub_queues[key] = queue
        _sub_workers[key] = asyncio.create_task(
            _worker(key), name=f"event_run_worker:{key}",
        )
    return queue


async def _worker(key: str) -> None:
    queue = _sub_queues[key]
    sem = _get_semaphore()
    while True:
        job: Optional[Dict[str, Any]] = await queue.get()
        if job is None:
            queue.task_done()
            break
        try:
            async with sem:
                await _execute(job)
        except Exception:  # noqa: BLE001
            logger.exception(f"[event_run] worker {key} failed on a job")
        finally:
            queue.task_done()


# ── public dispatch API (called from the three managers' fan-out points) ────

async def dispatch_skill_event(*, sub: Dict[str, Any], content: str) -> None:
    """Queue a fired skill-event subscription for an isolated run."""
    from app.agent.stream_runner import _format_trigger_content  # noqa: F401 (parity)

    skill_name = sub.get("skill_name", "")
    event_type = sub.get("event_type", "")
    action = (sub.get("action") or "").strip()
    query = f"{action}\n\n{content.strip()}".strip()
    job = {
        "source_kind": "skill_event",
        "subscription_id": sub["id"],
        "profile": sub["profile"],
        "registering_conversation_id": sub.get("conversation_id"),
        "label": f"{skill_name}:{event_type}" if skill_name else event_type,
        "action": action,
        "query": query,
        "trigger_event": {
            "event_type": event_type,
            "action": action,
            "content": content.strip(),
        },
        "trigger_payload": {
            "skill_name": skill_name,
            "event_type": event_type,
            "content_preview": content.strip()[:4000],
        },
        "user_metadata": {
            "source": "skill_event",
            "skill_name": skill_name,
            "event_type": event_type,
        },
        "gate": {
            "event_type": event_type,
            "action": action,
            "file_content": content,
        },
    }
    await _ensure_worker(_key("skill_event", sub["id"])).put(job)


async def dispatch_file_watcher_event(*, sub: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Queue a fired file-watcher subscription for an isolated run."""
    from app.events.file_watcher_runner import build_trigger_messages

    action = (sub.get("action") or "").strip()
    query, bubble = build_trigger_messages(action, payload)
    job = {
        "source_kind": "file_watcher",
        "subscription_id": sub["id"],
        "profile": sub["profile"],
        "registering_conversation_id": sub.get("conversation_id"),
        "label": sub.get("name") or "File watcher",
        "action": action,
        "query": query,
        "trigger_event": {
            "event_type": payload.get("event_type", "file"),
            "action": action,
            "content": bubble,
        },
        "trigger_payload": dict(payload),
        "user_metadata": {
            "source": "file_watcher_event",
            "watch_name": sub.get("name"),
        },
        "gate": None,
    }
    await _ensure_worker(_key("file_watcher", sub["id"])).put(job)


async def dispatch_schedule_event(
    *, sub: Dict[str, Any], action: str, payload: Dict[str, Any],
) -> None:
    """Queue a fired schedule subscription for an isolated run."""
    from app.events.schedule_event_runner import build_trigger_messages

    action = (action or "").strip()
    query, bubble = build_trigger_messages(action, payload)
    job = {
        "source_kind": "schedule",
        "subscription_id": sub["id"],
        "profile": sub["profile"],
        "registering_conversation_id": sub.get("conversation_id"),
        "label": sub.get("title") or action or "Schedule",
        "action": action,
        "query": query,
        "trigger_event": {
            "event_type": "schedule",
            "action": action,
            "content": bubble,
            "title": sub.get("title"),
            "schedule_kind": payload.get("schedule_kind"),
            "rrule": payload.get("rrule"),
            "fired_at": payload.get("fired_at"),
            "next_occurrence": payload.get("next_fire_at_iso"),
        },
        "trigger_payload": dict(payload),
        "user_metadata": {
            "source": "schedule_event",
            "subscription_id": sub["id"],
            "title": sub.get("title"),
            "schedule_kind": payload.get("schedule_kind"),
            "fired_at": payload.get("fired_at"),
        },
        "gate": None,
    }
    await _ensure_worker(_key("schedule", sub["id"])).put(job)


# ── execution ───────────────────────────────────────────────────────────────

async def _execute(job: Dict[str, Any]) -> None:
    from app.events import runner as event_runner
    from app.events.run_config import run_history_cap
    from app.storage import get_event_run_storage

    cremind_agent = event_runner.get_cremind_agent()
    conversation_storage = event_runner.get_conversation_storage()
    if cremind_agent is None or conversation_storage is None:
        logger.error("[event_run] runner globals not initialized; dropping trigger")
        return

    profile = job["profile"]
    source_kind = job["source_kind"]
    subscription_id = job["subscription_id"]
    label = job["label"]

    # ── Matching gate (skill events only) ──────────────────────────────────
    gate = job.get("gate")
    gate_llm: Any = None
    gate_tokens: dict = {}
    if gate is not None:
        from app.events.gate import classify_event_match
        try:
            gate_llm = cremind_agent.low_performance_llm(profile)
            result = await classify_event_match(
                llm=gate_llm,
                event_type=gate["event_type"],
                action=gate["action"],
                file_content=gate["file_content"],
            )
            matched, reason, gate_tokens = result.matched, result.reason, result.tokens
        except Exception:  # noqa: BLE001
            logger.exception("[event_run] gate failed; failing open (treating as match)")
            matched, reason, gate_tokens = True, "gate error; defaulted to match", {}
        if not matched:
            logger.info(
                f"[event_run] gate REJECTED sub={subscription_id} "
                f"reason={reason!r} — no run created"
            )
            # Rejected: record the gate cost with no conversation/run, then stop.
            await event_runner._record_gate_usage(
                llm=gate_llm, tokens=gate_tokens, conversation_id=None,
                profile=profile, event_type=gate["event_type"], message_id=None,
            )
            return

    # ── Create the hidden per-run conversation + run row ───────────────────
    try:
        conv = await conversation_storage.create_conversation(
            profile=profile, title=_run_title(label, profile), kind="event_run",
        )
        conversation_id = conv["id"]
    except Exception:  # noqa: BLE001
        logger.exception("[event_run] failed to create run conversation")
        return

    store = get_event_run_storage()
    try:
        created = await store.create(
            profile=profile,
            source_kind=source_kind,
            subscription_id=subscription_id,
            conversation_id=conversation_id,
            label=label,
            action=job.get("action", ""),
            trigger_payload=job.get("trigger_payload"),
            history_cap=run_history_cap(),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[event_run] failed to create run row")
        await _discard_conversation(conversation_id)
        return

    event_run_id = created["run"]["id"]
    # Retention cleanup: tear down pruned runs' conversations.
    for pruned_conv in created.get("pruned_conversation_ids", []):
        await _discard_conversation(pruned_conv)

    _publish_runs_changed(profile)

    # Matched-gate cost now attributes to the run.
    if gate is not None:
        await event_runner._record_gate_usage(
            llm=gate_llm, tokens=gate_tokens, conversation_id=conversation_id,
            profile=profile, event_type=gate["event_type"], message_id=None,
            event_run_id=event_run_id,
        )

    # Best-effort platform forwarding: if the rule was registered from an
    # external channel, mirror the run's reply back to that platform.
    await _maybe_forward_to_channel(
        conversation_storage, job.get("registering_conversation_id"), conversation_id,
    )

    # ── Run the agent in the hidden conversation ───────────────────────────
    from app.agent.stream_runner import make_run_id, run_agent_to_bus

    run_id = make_run_id(conversation_id, kind="event")
    try:
        await run_agent_to_bus(
            cremind_agent=cremind_agent,
            conversation_storage=conversation_storage,
            conversation_id=conversation_id,
            run_id=run_id,
            profile=profile,
            query=job["query"],
            history_messages=[],
            reasoning=True,
            user_message_metadata=job.get("user_metadata"),
            agent_message_metadata=job.get("user_metadata"),
            push_user_message=False,
            trigger_event=job.get("trigger_event"),
            publish_notification=True,
            update_title_from_query=False,
            event_run_id=event_run_id,
            event_run=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_run] agent run failed for {conversation_id}")
        try:
            await store.update_status(
                event_run_id, status="failed",
                error="Run failed to start", mark_finished=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[event_run] failed to mark run failed")
        _publish_runs_changed(profile)


def _run_title(label: str, profile: Optional[str] = None) -> str:
    from datetime import datetime
    from app.config.timezone import resolve_tzinfo
    stamp = datetime.now(resolve_tzinfo(profile)).strftime("%Y-%m-%d %H:%M")
    base = (label or "Event run").strip()
    return f"{base} · {stamp}"[:256]


async def _discard_conversation(conversation_id: Optional[str]) -> None:
    """Tear down a run conversation's in-memory queue/stream state + delete it."""
    from app.events.run_lifecycle import discard_run_conversation
    await discard_run_conversation(conversation_id)


async def _maybe_forward_to_channel(
    conversation_storage: Any,
    registering_conversation_id: Optional[str],
    run_conversation_id: str,
) -> None:
    if not registering_conversation_id:
        return
    try:
        conv = await conversation_storage.get_conversation(registering_conversation_id)
        channel_id = (conv or {}).get("channel_id")
        if not channel_id:
            return
        channel = await conversation_storage.get_channel(channel_id)
        channel_type = (channel or {}).get("channel_type")
        if channel and channel_type and channel_type != "main":
            from app.channels.registry import get_channel_registry
            adapter = get_channel_registry().get_adapter(channel_id)
            if adapter is not None:
                await adapter.forward_external_run(run_conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("[event_run] channel forwarder setup failed")


def _publish_runs_changed(profile: str) -> None:
    """Nudge the Events-page admin stream that a run changed (best-effort)."""
    try:
        from app.events.event_runs_admin_bus import publish_event_runs_changed
        publish_event_runs_changed(profile)
    except Exception:  # noqa: BLE001
        pass
