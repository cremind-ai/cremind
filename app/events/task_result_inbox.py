"""The transient half of a conversation's pending event-task results.

The DURABLE inbox is a query, not a structure: a terminal ``event_runs`` row
with ``deliver_to_origin`` and ``origin_delivered_at IS NULL`` already *is* a
pending result (see :meth:`EventRunStorage.list_pending_for_origin`). Parking a
result therefore writes nothing — the terminal-status write, which happens
strictly earlier, did it. That is what makes losing everything in this module on
restart the *correct* behaviour: the boot sweep re-delivers every parked row,
and a notice about a result that was already delivered would be a lie.

What cannot live in the DB, and so lives here:

**The run binding.** Built-in tools receive ``_context_id``, which is NOT the
conversation id for channel-backed conversations (those set an external
``context_id``), and a conversation id can be renamed, so it cannot be parsed
out of the run id either. ``bind_run`` records the mapping in both directions
while a turn is live; the reader tool resolves through it.

**The notice.** A short "a result arrived" line, drained onto the next tool
result the agent sees. Best-effort by design: if the turn takes no further tool
call, the notice is simply never shown and the turn-end flush injects the result
as a new turn instead. The notice is an optimisation; the flush is the
guarantee.

Why the binding — and not ``ConversationStreamBus.is_active`` — decides whether
a result parks: ``start_run`` sets ``_active`` outside the ``try`` that owns the
``finally``, so a stale True is reachable. Under an ``is_active`` fork a stale
flag would park every future result for that conversation with no turn-end hook
ever firing — a silent, indefinite deferral of something v1 delivered at once.
``bind_run`` is the first statement *inside* that ``try`` and ``unbind_run`` the
first statement of the ``finally``, so a binding exists only across a stretch
that is guaranteed to be torn down.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

# A pathological fan-out must not grow a conversation's notice list without
# bound; the oldest are dropped (the DB rows they describe are unaffected, and
# the turn-end flush delivers them all regardless).
_MAX_NOTICES = 20

_lock = threading.Lock()
# run_id -> conversation_id, and the reverse. Installed for the lifetime of one
# streamed turn.
_run_to_conv: Dict[str, str] = {}
_conv_to_run: Dict[str, str] = {}
# conversation_id -> [ {event_run_id, label, status_word} ], drained onto a tool
# result and cleared.
_notices: Dict[str, List[Dict[str, Any]]] = {}
# conversation_id -> True once anything parked, so the turn-end hook can skip a
# DB query on the overwhelming majority of turns. Cleared by reset().
_pending: Dict[str, bool] = {}
# run_id -> the deepest task_chain_depth consumed by a mid-turn read, so the
# reading turn inherits the chain cap it would otherwise escape.
_consumed_depth: Dict[str, int] = {}


# ── run binding ─────────────────────────────────────────────────────────────


def bind_run(run_id: str, conversation_id: str) -> None:
    """Mark a conversation as having a live turn. First statement of the try."""
    if not run_id or not conversation_id:
        return
    with _lock:
        _run_to_conv[run_id] = conversation_id
        _conv_to_run[conversation_id] = run_id


def unbind_run(run_id: str) -> None:
    """Drop the binding. First statement of the finally, before ``end_run``."""
    with _lock:
        conversation_id = _run_to_conv.pop(run_id, None)
        if conversation_id and _conv_to_run.get(conversation_id) == run_id:
            _conv_to_run.pop(conversation_id, None)
        _consumed_depth.pop(run_id, None)


def conversation_for_run(run_id: str) -> Optional[str]:
    """The conversation a live turn belongs to, or None outside one."""
    if not run_id:
        return None
    with _lock:
        return _run_to_conv.get(run_id)


# ── the fork ────────────────────────────────────────────────────────────────


def park_if_bound(conversation_id: str, notice: Dict[str, Any]) -> bool:
    """Park a finished result's notice IF the origin has a live turn.

    Deliberately ONE synchronous function rather than a liveness check the
    caller pairs with a separate park: the turn-end handoff is only sound
    because nothing can interleave between the two, and expressing them as one
    non-async call makes inserting an ``await`` there impossible rather than
    merely discouraged.

    Returns True when the notice was parked (caller returns ``PARKED`` and
    writes nothing); False when the conversation is idle, and the caller must
    deliver the result itself.
    """
    if not conversation_id:
        return False
    with _lock:
        if conversation_id not in _conv_to_run:
            return False
        queue = _notices.setdefault(conversation_id, [])
        queue.append(notice)
        if len(queue) > _MAX_NOTICES:
            del queue[: len(queue) - _MAX_NOTICES]
        _pending[conversation_id] = True
        return True


# ── notices ─────────────────────────────────────────────────────────────────


def drain_notices(run_id: str) -> List[Dict[str, Any]]:
    """Take every notice waiting for this run's conversation (drain-once)."""
    if not run_id:
        return []
    with _lock:
        conversation_id = _run_to_conv.get(run_id)
        if not conversation_id:
            return []
        return _notices.pop(conversation_id, []) or []


def has_pending(conversation_id: str) -> bool:
    """True if anything parked for this conversation since the last reset."""
    if not conversation_id:
        return False
    with _lock:
        return bool(_pending.get(conversation_id))


def reset(conversation_id: str) -> None:
    """Clear the pending marker after a successful turn-end flush."""
    with _lock:
        _pending.pop(conversation_id, None)
        _notices.pop(conversation_id, None)


def discard(conversation_id: str) -> None:
    """Forget a conversation entirely (deleted, reset, or id renamed)."""
    with _lock:
        _pending.pop(conversation_id, None)
        _notices.pop(conversation_id, None)
        run_id = _conv_to_run.pop(conversation_id, None)
        if run_id:
            _run_to_conv.pop(run_id, None)
            _consumed_depth.pop(run_id, None)


# ── chain depth carried by a mid-turn read ──────────────────────────────────


def note_consumed_depth(run_id: str, depth: int) -> None:
    """Record the deepest chain depth this turn read, for the registration cap.

    A turn that pulls a result mid-flight never mints a new ``trigger_event``,
    so without this it would restart the chain counter at zero and could ping-
    pong wait→read→register forever.
    """
    if not run_id:
        return
    with _lock:
        _consumed_depth[run_id] = max(_consumed_depth.get(run_id, 0), int(depth or 0))


def consumed_depth(run_id: str) -> int:
    if not run_id:
        return 0
    with _lock:
        return int(_consumed_depth.get(run_id, 0))


def clear_all() -> None:
    """Drop every entry. Tests only."""
    with _lock:
        _run_to_conv.clear()
        _conv_to_run.clear()
        _notices.clear()
        _pending.clear()
        _consumed_depth.clear()
