"""Per-conversation sequential user-message queue.

User-typed messages POSTed to ``/api/conversations/{id}/messages`` are processed
strictly sequentially per conversation: a worker awaits each agent run to
completion before draining the next. Different conversations run concurrently.
This keeps a conversation's storage/stream mutations from interleaving.

Event-triggered runs no longer use this queue — each fired trigger runs in its
own isolated conversation via :mod:`app.events.run_dispatcher` (per-rule FIFO +
global concurrency cap). A **reply** to a pending event run is an ordinary user
message on that run's conversation, so it flows through this queue like any
other chat turn (carrying ``event_run_id`` so the run's status/usage update).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.events import runner as event_runner
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
            # Lazy import: stream_runner pulls in app.events.notifications_buffer
            # which retriggers loading of app.events at module import time and
            # produces a circular import. Importing inside the worker avoids
            # that — by the time _worker actually runs (on the loop) all packages
            # are fully initialised.
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
                    attachments=item.get("attachments"),
                    push_user_message=item.get("push_user_message", True),
                    publish_notification=item.get("publish_notification", False),
                    update_title_from_query=item.get("update_title_from_query", True),
                    event_run_id=item.get("event_run_id"),
                    event_run=item.get("event_run", False),
                )
        except asyncio.CancelledError:
            # Intentional teardown (discard_queue) — exit cleanly.
            queue.task_done()
            raise
        except BaseException:  # noqa: BLE001
            # Never let one item kill the worker and wedge the conversation; a dead
            # worker is also self-healed by _ensure_worker on the next enqueue.
            logger.exception(
                f"Event queue: worker for conversation {conversation_id} failed on item"
            )
            queue.task_done()
            continue
        queue.task_done()


def _ensure_worker(conversation_id: str) -> asyncio.Queue:
    queue = _queues.get(conversation_id)
    if queue is None:
        queue = asyncio.Queue()
        _queues[conversation_id] = queue
    # Self-heal: (re)spawn the worker if it was never started or has died (e.g. an
    # unexpected error escaped the item handler) so a conversation never wedges.
    task = _workers.get(conversation_id)
    if task is None or task.done():
        task = asyncio.create_task(
            _worker(conversation_id), name=f"user_msg_worker:{conversation_id}",
        )
        _workers[conversation_id] = task
    return queue


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
    attachments: Optional[List[Dict[str, Any]]] = None,
    push_user_message: bool = True,
    update_title_from_query: bool = True,
    event_run_id: Optional[str] = None,
    event_run: bool = False,
    publish_notification: bool = False,
) -> None:
    """Add a user-typed message to the conversation's queue.

    ``attachments`` is the list of files the user uploaded with this message
    (``{"name", "path"}`` entries, paths already validated to live inside the
    conversation's temp upload dir). Their absolute paths are injected into the
    text the agent sees so it can read / convert / move them; they are kept out
    of the persisted/published user-message metadata (names-only) on purpose.

    ``event_run_id`` / ``event_run`` are set when the conversation is a hidden
    event-run conversation (a reply to a pending run) so the run's status and
    usage update and the ``request_user_input`` tool stays available.
    """
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
        "attachments": attachments,
        "push_user_message": push_user_message,
        "update_title_from_query": update_title_from_query,
        "event_run_id": event_run_id,
        "event_run": event_run,
        "publish_notification": publish_notification,
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
