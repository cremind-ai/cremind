"""Per-conversation working-directory override helpers.

Two storage layers cooperate:

* ``ContextStorage`` — in-memory, keyed by the conversation's ``context_id``.
  Read on every reasoning step by
  ``app.agent.reasoning_agent._build_instruction`` and by
  ``app.api.files`` for path-allowlist widening. Lost on server restart.

* ``conversations.working_directory`` (sqlite) — durable. Written every time
  the override is set or cleared via the HTTP endpoint or the
  ``change_working_directory`` tool. Source of truth across restarts.

The helpers below keep the two in sync:

* :func:`hydrate_working_directory` — called when a conversation becomes
  active (SSE subscribe, agent loop start). Loads the persisted value into
  ContextStorage if it isn't already there, validating that the path still
  exists on disk and is not inside another profile's working directory. Stale
  entries (target deleted, or now another profile's) are cleared and the
  conversation transparently falls back to its profile's own folder.

* :func:`persist_working_directory` — called by the writers (HTTP endpoint
  and tool) after they update ContextStorage, so the change survives a
  restart.

The override-key constant lives here so every callsite can import it from
one place — ``ContextStorage`` keys silently mismatching is the kind of bug
that's hard to spot and easy to introduce.

The two layers are addressed by *different* ids. For an ordinary conversation
``context_id == conversation_id``, which is why they were long used
interchangeably; a group-chat seat breaks that (its ``context_id`` is the
literal ``group:<gid>:<profile>`` string, not a row id). :func:`resolve_cwd_scope`
maps either half onto both, so the durable write lands on a real row while the
in-memory value stays under the key the agent reads.
"""

from __future__ import annotations

import os
from typing import Any

from app.config.settings import get_user_working_directory
from app.config.working_dirs import is_foreign
from app.utils.context_storage import (
    clear_context,
    get_context,
    set_context,
)
from app.utils.logger import logger


WORKING_DIR_OVERRIDE_KEY = "_working_directory_override"

# The one sentence every surface answers with when a directory it was handed
# lies inside another profile's working directory.
FOREIGN_WORKING_DIR_MESSAGE = "That folder belongs to another profile."


def resolve_tool_cwd(
    arguments: dict,
    *,
    explicit_key: str | None = "working_directory",
) -> tuple[str | None, str | None]:
    """``(cwd, error)`` for a built-in tool that runs a process in a directory
    (the coding agents): the model's explicit ``arguments[explicit_key]``, else
    the adapter-injected ``_working_directory``, else the calling profile's own
    folder. Absolute and ``~``-expanded, not created.

    ``error`` (and no cwd) when nothing names a directory and no ``_profile``
    says whose folder to use, or when the directory lies inside ANOTHER
    profile's working directory — the admin is not exempt. The process itself
    runs as the server's OS user and cannot be sandboxed; this only stops the
    tool from being pointed there."""
    profile = arguments.get("_profile") or None
    raw = (arguments.get(explicit_key) if explicit_key else None) or arguments.get(
        "_working_directory"
    )
    if not raw:
        if not profile:
            return None, "No working directory was given and the calling profile is unknown."
        raw = get_user_working_directory(profile)
    cwd = os.path.abspath(os.path.expanduser(str(raw)))
    if is_foreign(cwd, profile):
        return None, f"Cannot use '{cwd}' as the working directory: {FOREIGN_WORKING_DIR_MESSAGE}"
    return cwd, None


async def _try_load(conv_storage: Any, method: str, *args: Any) -> dict | None:
    """Call one conversation-lookup method defensively.

    :func:`resolve_cwd_scope` runs on the cwd write path of every tool call, so
    a storage that is missing (tests pass narrow fakes), not yet initialised, or
    simply erroring must degrade to "unresolved" rather than abort the switch.
    """
    if conv_storage is None:
        return None
    fn = getattr(conv_storage, method, None)
    if fn is None:
        return None
    try:
        return await fn(*args)
    except Exception:  # noqa: BLE001
        logger.debug(
            f"resolve_cwd_scope: {method}{args!r} failed", exc_info=True,
        )
        return None


async def resolve_cwd_scope(
    conv_storage: Any,
    *,
    conversation_id: str | None = None,
    context_id: str | None = None,
    profile: str | None = None,
) -> tuple[str, str]:
    """Map either half of a conversation's identity onto ``(row_id, context_key)``.

    ``row_id`` addresses the durable column and the event-stream channel;
    ``context_key`` addresses the in-memory override. They diverge for a
    group-chat seat, whose ``context_id`` ("group:<gid>:<profile>") matches no
    conversation row — persisting under it updates nothing and publishing under
    it reaches nobody, while keying the in-memory value by the row id instead
    hides the seat's cwd from the agent that reads it.

    Pass ``conversation_id`` when the caller holds a row id (the HTTP endpoints)
    and ``context_id`` + ``profile`` when it holds an agent-loop context (the
    tools). Resolution never raises: an id that resolves to no row falls back to
    itself for both halves, which is exactly the pre-seat behaviour.
    """
    if conversation_id:
        conv = await _try_load(conv_storage, "get_conversation", conversation_id)
        if conv:
            return (
                conv.get("id") or conversation_id,
                conv.get("context_id") or conv.get("id") or conversation_id,
            )
        return conversation_id, conversation_id

    if not context_id:
        return "", ""

    # Same two-step the compaction tool uses: a seat is only findable by
    # (profile, context_id); an ordinary conversation whose context_id was
    # never back-filled is findable by id alone.
    conv = None
    if profile:
        conv = await _try_load(
            conv_storage, "get_conversation_by_context", profile, context_id,
        )
    if conv is None:
        conv = await _try_load(conv_storage, "get_conversation", context_id)
    if conv:
        return conv.get("id") or context_id, context_id
    return context_id, context_id


async def _load_conversation(conv_storage: Any, conversation_id: str) -> dict | None:
    if conv_storage is None:
        return None
    try:
        return await conv_storage.get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"hydrate_working_directory: get_conversation failed for "
            f"{conversation_id}"
        )
        return None


def _profile_default(profile: str | None, conversation_id: str | None) -> str:
    """The owning profile's own folder. With no profile there is no folder to
    fall back to — guessing ``admin`` would hand one profile another's files."""
    if not profile:
        raise ValueError(
            f"hydrate_working_directory: no profile for conversation "
            f"{conversation_id!r}; cannot resolve its working directory"
        )
    return get_user_working_directory(profile)


async def hydrate_working_directory(
    conversation_id: str,
    conv_storage: Any,
    *,
    context_key: str | None = None,
    profile: str | None = None,
) -> str:
    """Ensure ContextStorage holds the conversation's persisted override and
    return the conversation's effective working directory.

    Order of precedence:

    1. Existing in-memory ContextStorage value (already hydrated this boot).
    2. ``conversations.working_directory`` from the DB.
    3. ``get_user_working_directory(profile)`` (the profile's own folder).

    The owning profile is the conversation row's ``profile``, else the
    ``profile`` argument; with neither the fallback raises ``ValueError``
    rather than guess one.

    An override that no longer points at a real directory, or that now lies
    inside ANOTHER profile's working directory (the admin moved a folder), is
    cleared from both stores so the next reasoning step sees the default.

    The DB row is always addressed by ``conversation_id``; ``context_key``
    (default: the same id) is the ContextStorage key the agent reads the
    override back under. A group-chat seat needs the two to differ, otherwise
    its restored cwd lands under an id its reasoning loop never looks at.

    Returns the path the agent should treat as the user's current working
    directory.
    """
    if not conversation_id:
        return _profile_default(profile, conversation_id)

    key = context_key or conversation_id
    owner = profile
    conv: dict | None = None
    loaded = False
    in_memory = get_context(key, WORKING_DIR_OVERRIDE_KEY)
    if isinstance(in_memory, str) and in_memory:
        if not owner:
            conv, loaded = await _load_conversation(conv_storage, conversation_id), True
            owner = (conv or {}).get("profile") or None
        if not is_foreign(in_memory, owner):
            return in_memory
        logger.info(
            f"hydrate_working_directory: cwd {in_memory!r} of {conversation_id} "
            f"is inside another profile's working directory; dropping it"
        )
        clear_context(key, WORKING_DIR_OVERRIDE_KEY)

    if not loaded:
        conv = await _load_conversation(conv_storage, conversation_id)
    owner = (conv or {}).get("profile") or owner

    persisted: str | None = None
    if conv:
        wd = conv.get("working_directory")
        if isinstance(wd, str) and wd:
            persisted = wd

    if persisted:
        if not os.path.isdir(persisted):
            reason = "no longer exists"
        elif is_foreign(persisted, owner):
            reason = "is inside another profile's working directory"
        else:
            set_context(key, WORKING_DIR_OVERRIDE_KEY, persisted)
            return persisted
        # Stale: clear so we don't keep retrying it.
        logger.info(
            f"hydrate_working_directory: persisted cwd {persisted!r} for "
            f"{conversation_id} {reason}; falling back to the profile's own folder"
        )
        if conv_storage is not None:
            try:
                await conv_storage.update_conversation(
                    conversation_id, working_directory=None,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"hydrate_working_directory: failed to clear stale cwd for "
                    f"{conversation_id}"
                )

    return _profile_default(owner, conversation_id)


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
            f"persist_working_directory: update_conversation failed for "
            f"{conversation_id}"
        )


def clear_in_memory_override(context_key: str) -> None:
    """Convenience wrapper around ``clear_context`` for the override key."""
    if not context_key:
        return
    clear_context(context_key, WORKING_DIR_OVERRIDE_KEY)


async def switch_conversation_cwd(
    context_key: str,
    path: str,
    conv_storage: Any,
    *,
    profile: str | None = None,
    publish: bool = True,
) -> None:
    """Point a conversation's working directory at *path* (an existing dir).

    Performs the full in-memory + durable + notify tail shared by the
    ``change_working_directory`` tool and the adapter's sandbox auto-recovery:

    1. set the in-memory ContextStorage override under ``context_key`` (read on
       the next reasoning step and by every built-in tool call this turn);
    2. persist it to ``conversations.working_directory`` so it survives restart;
    3. publish a ``cwd`` event on the conversation's event-stream bus so any
       subscribed UI (the Vue ``CwdBreadcrumb``, the CLI tree) re-renders.

    Steps 2 and 3 address the conversation *row*, which ``context_key`` only is
    for a non-seat conversation — hence the :func:`resolve_cwd_scope` hop, for
    which ``profile`` narrows a seat lookup.

    Persistence and publish failures are logged, not raised — the in-memory
    value still drives the current run.
    """
    if not context_key or not path:
        return
    set_in_memory_override(context_key, path)
    row_id, _ = await resolve_cwd_scope(
        conv_storage, context_id=context_key, profile=profile,
    )
    await persist_working_directory(row_id, path, conv_storage)
    if publish:
        try:
            from app.events import get_event_stream_bus
            await get_event_stream_bus().publish(
                row_id, "cwd", {"working_directory": path},
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"switch_conversation_cwd: failed to publish cwd event for {row_id}"
            )


def set_in_memory_override(context_key: str, path: str) -> None:
    """Convenience wrapper around ``set_context`` for the override key."""
    if not context_key or not path:
        return
    set_context(context_key, WORKING_DIR_OVERRIDE_KEY, path)
