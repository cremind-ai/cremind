"""Per-conversation working-directory override helpers.

Two storage layers cooperate:

* ``ContextStorage`` — in-memory, keyed by ``conversation_id`` (== ``context_id``
  inside the agent loop). Read on every reasoning step by
  ``app.agent.reasoning_agent._build_instruction`` and by
  ``app.api.files`` for path-allowlist widening. Lost on server restart.

* ``conversations.working_directory`` (sqlite) — durable. Written every time
  the override is set or cleared via the HTTP endpoint or the
  ``change_working_directory`` tool. Source of truth across restarts.

The helpers below keep the two in sync:

* :func:`hydrate_working_directory` — called when a conversation becomes
  active (SSE subscribe, agent loop start). Loads the persisted value into
  ContextStorage if it isn't already there, validating that the path still
  exists on disk. Stale entries (target deleted) are cleared and the
  conversation transparently falls back to the user default.

* :func:`persist_working_directory` — called by the writers (HTTP endpoint
  and tool) after they update ContextStorage, so the change survives a
  restart.

The override-key constant lives here so every callsite can import it from
one place — ``ContextStorage`` keys silently mismatching is the kind of bug
that's hard to spot and easy to introduce.
"""

from __future__ import annotations

import os
from typing import Any

from app.config.settings import get_user_working_directory
from app.utils.context_storage import (
    clear_context,
    get_context,
    set_context,
)
from app.utils.logger import logger


WORKING_DIR_OVERRIDE_KEY = "_working_directory_override"


async def hydrate_working_directory(
    conversation_id: str,
    conv_storage: Any,
) -> str:
    """Ensure ContextStorage holds the conversation's persisted override and
    return the conversation's effective working directory.

    Order of precedence:

    1. Existing in-memory ContextStorage value (already hydrated this boot).
    2. ``conversations.working_directory`` from the DB.
    3. ``get_user_working_directory()`` (the profile default).

    A persisted override that no longer points at a real directory is
    cleared from both stores so the next reasoning step sees the default.

    Returns the path the agent should treat as the user's current working
    directory.
    """
    if not conversation_id:
        return get_user_working_directory()

    in_memory = get_context(conversation_id, WORKING_DIR_OVERRIDE_KEY)
    if isinstance(in_memory, str) and in_memory:
        return in_memory

    persisted: str | None = None
    if conv_storage is not None:
        try:
            conv = await conv_storage.get_conversation(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "hydrate_working_directory: get_conversation failed for %s",
                conversation_id,
            )
            conv = None
        if conv:
            wd = conv.get("working_directory")
            if isinstance(wd, str) and wd:
                persisted = wd

    if persisted:
        if os.path.isdir(persisted):
            set_context(conversation_id, WORKING_DIR_OVERRIDE_KEY, persisted)
            return persisted
        # Stale: directory is gone. Clear so we don't keep retrying it.
        logger.info(
            "hydrate_working_directory: persisted cwd %r for %s no longer "
            "exists; falling back to user default",
            persisted, conversation_id,
        )
        if conv_storage is not None:
            try:
                await conv_storage.update_conversation(
                    conversation_id, working_directory=None,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "hydrate_working_directory: failed to clear stale cwd "
                    "for %s", conversation_id,
                )

    return get_user_working_directory()


async def persist_working_directory(
    conversation_id: str,
    path: str | None,
    conv_storage: Any,
) -> None:
    """Write the override to durable storage.

    ``path=None`` clears the override (used by ``change_working_directory``
    when the tool's target is ``user_working``). The in-memory
    ContextStorage value is updated by the caller — this helper only
    handles persistence so failures here don't disrupt the in-memory state
    the agent reads on its next step.
    """
    if not conversation_id or conv_storage is None:
        return
    try:
        await conv_storage.update_conversation(
            conversation_id, working_directory=path,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "persist_working_directory: update_conversation failed for %s",
            conversation_id,
        )


def clear_in_memory_override(conversation_id: str) -> None:
    """Convenience wrapper around ``clear_context`` for the override key."""
    if not conversation_id:
        return
    clear_context(conversation_id, WORKING_DIR_OVERRIDE_KEY)


def set_in_memory_override(conversation_id: str, path: str) -> None:
    """Convenience wrapper around ``set_context`` for the override key."""
    if not conversation_id or not path:
        return
    set_context(conversation_id, WORKING_DIR_OVERRIDE_KEY, path)
