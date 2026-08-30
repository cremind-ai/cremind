"""Each member's seat in a group: a hidden conversation of its own.

A group message does not go into some shared thread — it is delivered into one
ordinary ``conversations`` row per member, marked ``kind='group_chat'``. That
choice is what makes the whole feature small: the per-conversation queue already
serialises a member's turns, the stream bus already carries them, mid-turn
injection already folds in whatever arrives while it is thinking, compaction
already trims its history, and its tools, persona and LLM are already its own.
Nothing about running an agent had to be re-invented for a room.

The seat's ``context_id`` includes the profile (``group:<id>:<profile>``). That
is not cosmetic: ``context_id`` keys the per-conversation tool state — working
directory, loaded skills, the current query — so seats sharing one id would let
Dog's ``change_working_directory`` silently move Cat's, across a tenant boundary.

The membership row holds the seat's id, and creation runs under a per-seat lock,
so two messages arriving at once cannot produce two seats for one member.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from app.groups.constants import CONTEXT_PREFIX, GROUP_CONVERSATION_KIND
from app.utils.logger import logger

_locks: Dict[tuple, asyncio.Lock] = {}


def shadow_context_id(group_id: str, profile: str) -> str:
    return f"{CONTEXT_PREFIX}{group_id}:{profile}"


def group_id_from_context(context_id: Optional[str]) -> Optional[str]:
    """``group:<gid>:<profile>`` → ``<gid>``, or ``None`` for anything else."""
    if not context_id or not str(context_id).startswith(CONTEXT_PREFIX):
        return None
    rest = str(context_id)[len(CONTEXT_PREFIX):]
    gid = rest.split(":", 1)[0]
    return gid or None


def _lock_for(group_id: str, profile: str) -> asyncio.Lock:
    key = (group_id, profile)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def ensure_shadow_conversation(
    conversation_storage: Any, profile: str, group: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return this member's seat, creating it on first use.

    Resolution order — membership pointer, then ``context_id`` lookup, then
    create — so a pointer lost to a half-finished migration or a manual delete
    self-heals instead of spawning a duplicate seat.
    """
    group_id = group.get("id")
    if not group_id or not profile:
        return None
    from app.storage import get_group_chat_storage

    storage = get_group_chat_storage()
    index = _index()

    async with _lock_for(group_id, profile):
        member = await storage.get_member(group_id, profile)
        if member is None:
            logger.warning(
                f"[group] {profile} is not a member of {group_id}; no seat created"
            )
            return None

        conv_id = member.get("shadow_conversation_id")
        if conv_id:
            existing = await conversation_storage.get_conversation(conv_id)
            if existing is not None:
                if index is not None:
                    index.note_shadow_conversation(group_id, profile, conv_id)
                return existing

        context_id = shadow_context_id(group_id, profile)
        try:
            existing = await conversation_storage.get_conversation_by_context(
                profile, context_id,
            )
        except Exception:  # noqa: BLE001 - duplicate rows would raise here
            logger.exception(
                f"[group] ambiguous seat lookup for {profile} in {group_id}"
            )
            existing = None
        if existing is None:
            existing = await conversation_storage.create_conversation(
                profile=profile,
                context_id=context_id,
                title=f"Group: {group.get('name') or group_id}",
                kind=GROUP_CONVERSATION_KIND,
            )
        await storage.set_shadow_conversation(group_id, profile, existing["id"])
        if index is not None:
            index.note_shadow_conversation(group_id, profile, existing["id"])
        return existing


async def delete_shadow_conversation(
    conversation_storage: Any, group_id: str, profile: str,
    conversation_id: Optional[str] = None,
) -> None:
    """Tear a seat down completely — runtime state first, then the row.

    Mirrors :func:`app.events.run_lifecycle.discard_run_conversation`: dropping
    only the row would leave a queue worker, a stream-bus entry and possibly a
    run binding pointing at a conversation that no longer exists.
    """
    from app.storage import get_group_chat_storage

    storage = get_group_chat_storage()
    conv_id = conversation_id
    if not conv_id:
        member = await storage.get_member(group_id, profile)
        conv_id = (member or {}).get("shadow_conversation_id")
    if conv_id:
        try:
            from app.events import queue as event_queue
            from app.events import task_result_inbox
            from app.events.stream_bus import get_event_stream_bus

            event_queue.discard_queue(conv_id)
            await get_event_stream_bus().discard(conv_id)
            task_result_inbox.discard(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"[group] failed to discard runtime state for {conv_id}")
        try:
            await conversation_storage.delete_conversation(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"[group] failed to delete seat conversation {conv_id}")
    try:
        await storage.set_shadow_conversation(group_id, profile, None)
    except Exception:  # noqa: BLE001
        logger.debug("[group] could not clear the seat pointer", exc_info=True)
    index = _index()
    if index is not None:
        index.note_shadow_conversation(group_id, profile, None)


def _index():
    try:
        from app.groups.index import get_group_index

        return get_group_index()
    except Exception:  # noqa: BLE001
        return None
