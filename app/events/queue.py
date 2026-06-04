"""Per-conversation sequential agent run queue.

Runs for the same conversation are processed strictly sequentially: a worker
awaits each agent run to completion before draining the next item. Different
conversations run concurrently.

Three run kinds share this queue:

* ``"skill_event"`` — synthesised events from a watched skill folder; the
  worker delegates to :func:`app.events.runner.run_event`.
* ``"file_watcher_event"`` — filesystem events from a registered watch root;
  the worker delegates to :func:`app.events.file_watcher_runner.run_event`.
* ``"user_message"`` — user-typed messages POSTed to
  ``/api/conversations/{id}/messages``; the worker delegates to
  :func:`app.agent.stream_runner.run_agent_to_bus` directly.

Sharing a single per-conversation queue means a user message and an
event-triggered run for the same conversation never interleave their
mutations to storage or the stream bus's ring buffer.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.events import runner as event_runner
from app.events import file_watcher_runner
from app.utils.logger import logger


_queues: Dict[str, asyncio.Queue] = {}
_workers: Dict[str, asyncio.Task] = {}


async def _worker(conversation_id: str) -> None:
    queue = _queues[conversation_id]
    while True:
        item: Dict[str, Any] | None = await queue.get()
        if item is None:
            queue.task_done()
            break
        try:
            kind = item.get("kind", "skill_event")
            if kind == "skill_event":
                await event_runner.run_event(
                    conversation_id=conversation_id,
                    profile=item["profile"],
                    skill_name=item["skill_name"],
                    event_type=item["event_type"],
                    action=item["action"],
                    file_content=item["file_content"],
                )
            elif kind == "file_watcher_event":
                await file_watcher_runner.run_event(
                    conversation_id=conversation_id,
                    profile=item["profile"],
                    subscription_id=item["subscription_id"],
                    watch_name=item["watch_name"],
                    action=item["action"],
                    payload=item["payload"],
                )
            elif kind == "user_message":
                # Lazy import: stream_runner pulls in app.events.notifications_buffer
                # which retriggers loading of app.events at module import time
                # and produces a circular import. Importing inside the worker
                # avoids that — by the time _worker actually runs (on the loop)
                # all packages are fully initialised.
                from app.agent.stream_runner import run_agent_to_bus
                cremind_agent = event_runner.get_cremind_agent()
                conversation_storage = event_runner.get_conversation_storage()
                if cremind_agent is None or conversation_storage is None:
                    logger.error(
                        "Queue worker: globals not initialized; dropping user message"
                    )
                else:
                    await run_agent_to_bus(
                        cremind_agent=cremind_agent,
                        conversation_storage=conversation_storage,
                        conversation_id=conversation_id,
                        run_id=item["run_id"],
                        profile=item["profile"],
                        query=item["query"],
                        history_messages=item.get("history_messages") or [],
                        reasoning=item.get("reasoning", True),
                        user_parts=item.get("user_parts"),
                        user_message_metadata=item.get("user_message_metadata"),
                        agent_message_metadata=item.get("agent_message_metadata"),
                        push_user_message=item.get("push_user_message", True),
                        publish_notification=False,
                        update_title_from_query=item.get(
                            "update_title_from_query", True,
                        ),
                    )
            else:
                logger.warning(
                    f"Queue worker: unknown run kind {kind!r} for {conversation_id}"
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Event queue: worker for conversation {conversation_id} failed on item"
            )
        finally:
            queue.task_done()


def _ensure_worker(conversation_id: str) -> asyncio.Queue:
    queue = _queues.get(conversation_id)
    if queue is None:
        queue = asyncio.Queue()
        _queues[conversation_id] = queue
        task = asyncio.create_task(
            _worker(conversation_id), name=f"event_worker:{conversation_id}",
        )
        _workers[conversation_id] = task
    return queue


async def enqueue(
    *,
    conversation_id: str,
    profile: str,
    skill_name: str,
    event_type: str,
    action: str,
    file_content: str,
) -> None:
    """Add a skill event to the conversation's queue (worker is created lazily)."""
    queue = _ensure_worker(conversation_id)
    await queue.put({
        "kind": "skill_event",
        "profile": profile,
        "skill_name": skill_name,
        "event_type": event_type,
        "action": action,
        "file_content": file_content,
    })


async def enqueue_file_watcher_event(
    *,
    conversation_id: str,
    profile: str,
    subscription_id: str,
    watch_name: str,
    action: str,
    payload: Dict[str, Any],
) -> None:
    """Add a file-watcher event to the conversation's queue."""
    queue = _ensure_worker(conversation_id)
    await queue.put({
        "kind": "file_watcher_event",
        "profile": profile,
        "subscription_id": subscription_id,
        "watch_name": watch_name,
        "action": action,
        "payload": payload,
    })


async def enqueue_user_message(
    *,
    conversation_id: str,
    run_id: str,
    profile: str,
    query: str,
    history_messages: Optional[List[Any]] = None,
    reasoning: bool = True,
    user_parts: Optional[List[Any]] = None,
    user_message_metadata: Optional[Dict[str, Any]] = None,
    agent_message_metadata: Optional[Dict[str, Any]] = None,
    push_user_message: bool = True,
    update_title_from_query: bool = True,
) -> None:
    """Add a user-typed message to the conversation's queue."""
    queue = _ensure_worker(conversation_id)
    await queue.put({
        "kind": "user_message",
        "run_id": run_id,
        "profile": profile,
        "query": query,
        "history_messages": history_messages or [],
        "reasoning": reasoning,
        "user_parts": user_parts,
        "user_message_metadata": user_message_metadata,
        "agent_message_metadata": agent_message_metadata,
        "push_user_message": push_user_message,
        "update_title_from_query": update_title_from_query,
    })


def discard_queue(conversation_id: str) -> None:
    """Stop and forget the worker for a conversation (e.g., on conversation delete)."""
    queue = _queues.pop(conversation_id, None)
    task = _workers.pop(conversation_id, None)
    if queue is not None:
        try:
            queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
    if task is not None and not task.done():
        task.cancel()
