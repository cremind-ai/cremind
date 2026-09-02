"""Returning an event run's result to the conversation that registered the rule.

Every rule registered from a real conversation reports back: a ONE-SHOT task the
agent registered mid-flow because it cannot finish without the outcome ("open the
PR, wait for CI, then merge"), and a STANDING rule that reports after each firing
("summarize every new email", "every day at 8 PM compile the news"). The two
differ in what happens next, not in whether the result arrives: a one-shot
continues the flow that was waiting on it and terminates its subscription, while
a standing rule's result is acted on and reported, and the rule stays armed.

Only a run with nowhere to report is silent — a rule bound to a reserved host
(the calendar UI's ``__schedule__``, the blueprint import host) or to a
conversation that has since been deleted. Those surface as notifications, as
before. :func:`app.events.task_policy.is_deliverable_origin` draws that line and
the run dispatcher freezes the answer onto the run at fire time.

*How* it comes back depends on what the origin is doing at that moment, because
one conversation runs one turn at a time (``app/events/queue.py``): a result
injected while the agent is mid-reasoning would sit in the FIFO behind that
turn, invisible until it ended.

**Origin idle** — the result is injected as a continuation turn, and any
sibling results still waiting come with it in ONE coalesced turn.

**Origin mid-turn** — the result *parks*: a short notice interrupts the running
turn at its next step (the same channel a mid-turn user message uses, down to
the brief reply the user gets), and the agent decides whether to pull the full
text now with ``get_event_task_results`` or keep working. Whatever it did not
read is injected as one coalesced turn the moment the turn ends. Parking writes
nothing to the DB, because the terminal-status write already made the row a
pending inbox entry (``deliver_to_origin`` + ``origin_delivered_at IS NULL``).

Four properties this module is responsible for:

**Exactly once.** :meth:`EventRunStorage.claim_delivery` is a conditional UPDATE
on ``origin_delivered_at IS NULL`` — the single arbiter, shared by every
consumer (mid-turn read, idle injection, turn-end flush, boot sweep). The
busy/idle fork is a heuristic about latency and never a correctness decision: if
it guesses wrong, two consumers race on that one UPDATE and exactly one wins.
A result is either handed to a running turn or in an injected turn, never both.

**Never resurrected.** ``clear_delivery_claim`` is reachable only for a claim
the *same call* took moments earlier (an enqueue that raised, a read that was
cancelled). A row claimed by a sibling flush must resolve to
:data:`ALREADY_DELIVERED` — releasing it there would re-deliver the result on
every boot, forever.

**Every terminal path.** The hook lives in ``stream_runner``'s terminal
finalize rather than in the dispatcher, so it also fires for a task run that
parked as ``pending`` (``request_user_input``), was answered by a human hours
later, and only then completed — that terminal comes from a different call
stack entirely.

**Never a silent hang.** A failed run, a run interrupted by a restart, a task
whose event never fired before its deadline, a notice the agent ignored: each
delivers *something* back, through four backstops (mid-turn read, turn-end
flush, any later turn's flush, boot sweep). A ONE-SHOT result is never dropped
by the volume bounds below for the same reason.

**Bounded.** A standing rule can fire faster than its conversation answers, and
an outage can leave a week of them owed. Only the newest
``max_results_per_delivery`` standing results are reported in one turn and only
those younger than ``undelivered_max_age_hours``; the rest are closed out (they
remain in the run history) and the turn says which rules they came from.
"""

from __future__ import annotations

import time
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

from app.events import task_result_inbox
from app.utils.logger import logger


class FlushOutcome(NamedTuple):
    """What one :func:`flush_origin_inbox` call did.

    ``claimed`` holds only the ids THIS call claimed and ENQUEUED, so a caller
    can tell "I delivered my row" from "a sibling flush already took it" — the
    distinction that stops a coalesced boot sweep from releasing a claim it does
    not own. ``dropped`` holds the ids this call claimed and closed out without
    reporting (stale, or over the per-delivery cap); the two sets are disjoint,
    because a row in ``claimed`` produces a message and a row in ``dropped``
    never will. ``origin_gone`` is separate because it is not a delivery
    failure: the results were closed out permanently, and the run's own
    notification must stay so the outcome is not lost entirely.
    """

    claimed: Set[str]
    origin_gone: bool = False
    dropped: FrozenSet[str] = frozenset()

# Return values of :func:`on_run_terminal`, so callers can branch without
# re-reading the DB (the stream runner uses them to suppress the run's own
# notification when the origin conversation is about to speak instead).
DELIVERED = "delivered"
NOT_TASK = "not_task"
ALREADY_DELIVERED = "already_delivered"
ORIGIN_GONE = "origin_gone"
SKIPPED_CANCELLED = "skipped_cancelled"
FAILED = "failed"
#: The origin conversation was mid-turn: the result waits in its inbox for the
#: agent to read, or for the turn-end flush. No DB write was taken.
PARKED = "parked"
#: Closed out without reporting: too old, or past the per-delivery cap for its
#: conversation. Deliberately NOT in :data:`SUPPRESSES_RUN_NOTIFICATION` — the
#: origin will never speak for this result, so the hidden run's own notification
#: is the only trace left and must stay.
SKIPPED_STALE = "skipped_stale"

#: Outcomes where the origin conversation itself raises the user-facing
#: notification, so the hidden run must not raise a second one. ``PARKED``
#: qualifies: the origin speaks for the result either way, via the agent's own
#: turn or via the flush that follows it.
SUPPRESSES_RUN_NOTIFICATION = (DELIVERED, SKIPPED_CANCELLED, PARKED)

#: How a claimed result was handed over (``event_runs.origin_delivery_mode``).
MODE_INJECTED = "injected"
MODE_READ = "read"
MODE_SKIPPED = "skipped"

_NO_OUTPUT = "(the run produced no output)"

_SOURCE_KIND_WORDS = {
    "skill_event": "skill event",
    "file_watcher": "file watcher",
    "schedule": "schedule",
}


# ── shape of a pending row ──────────────────────────────────────────────────


def _is_once(run: Dict[str, Any]) -> bool:
    """Whether this run belongs to a ONE-SHOT task, frozen at fire time.

    The rule row is editable and deletable, so the run carries its own answer
    (``trigger_payload["once"]``, written by the dispatcher). A row from before
    that key existed falls back to ``deliver_to_origin``: back then only a
    one-shot task ever delivered, so an old delivering row IS one — which keeps
    its wording, its chain depth and its subscription termination correct
    through the upgrade.
    """
    payload = run.get("trigger_payload") or {}
    if "once" in payload:
        return bool(payload["once"])
    return bool(run.get("deliver_to_origin"))


def _terminal_ts(run: Dict[str, Any]) -> float:
    """When this run's result became available (epoch ms)."""
    for key in ("finished_at", "updated_at", "created_at"):
        value = run.get(key)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _stale_cutoff_ms() -> Optional[float]:
    """Results finishing before this are not worth reporting. None = no bound."""
    from app.events.run_config import undelivered_max_age_hours

    hours = undelivered_max_age_hours()
    if hours <= 0:
        return None
    return time.time() * 1000 - hours * 3600 * 1000


def _partition_pending(
    rows: List[Dict[str, Any]], *, cap: int, cutoff_ms: Optional[float],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split an inbox into what to report now and what to close out.

    ``rows`` arrive oldest-finished first and come back in the same order.

    Both bounds apply to STANDING results only. A one-shot result was
    explicitly awaited by a flow that cannot continue without it — dropping one
    silently would strand that flow forever, and its ``timeout_minutes`` is
    already the deadline that governs it. So a one-shot row is always kept,
    however old, and never counts against the cap.
    """
    fresh: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for row in rows:
        if cutoff_ms is not None and not _is_once(row) and _terminal_ts(row) < cutoff_ms:
            dropped.append(row)
        else:
            fresh.append(row)

    recurring = [row for row in fresh if not _is_once(row)]
    if len(recurring) > cap:
        over_cap = {id(row) for row in recurring[: len(recurring) - cap]}
        keep = [row for row in fresh if id(row) not in over_cap]
        dropped += [row for row in fresh if id(row) in over_cap]
    else:
        keep = fresh
    dropped.sort(key=_terminal_ts)
    return keep, dropped


def _dropped_line(items: List[Dict[str, Any]]) -> str:
    """Name the results that were closed out, so the agent can say what was lost."""
    if not items:
        return ""
    named = "; ".join(
        f"{it['run'].get('label') or 'automation'} ({status_word_for(it['status'])})"
        for it in items
    )
    n = len(items)
    return (
        f"\n\n{n} older result{'s' if n != 1 else ''} for this conversation "
        f"{'were' if n != 1 else 'was'} dropped without being reported — too old, "
        f"or past the per-conversation limit: {named}. Mention this if it matters; "
        "the full history is on the Events page (`cremind event-runs list`)."
    )


def _room_line() -> str:
    """Licence to speak, for a result landing in a room rather than a 1:1 chat."""
    return (
        "\n\nThis conversation is a room: your answer is posted to everyone in it. "
        "This result is YOUR OWN automation reporting back — the room asked for it "
        "when the rule was set up — so report the outcome in your own words. Answer "
        "exactly [silent] only if the result says there is nothing new to report."
    )


# ── message construction ────────────────────────────────────────────────────


def status_word_for(status: str, *, timed_out: bool = False) -> str:
    """The one word the model and the user both see for a run's outcome."""
    if timed_out:
        return "timed out"
    return "completed" if status == "completed" else "failed"


def build_result_block(
    run: Dict[str, Any],
    *,
    status: str,
    result_text: str,
    timed_out: bool = False,
) -> str:
    """One result's ``Awaited / Task action / Status / …`` body.

    Pure, and THE single place an outcome is worded: the mid-turn read, the idle
    injection and the coalesced turn-end injection all render through it, so
    their instructions to the model cannot drift apart.

    A ONE-SHOT result continues the flow that was waiting on it. A STANDING
    rule's result is acted on and reported — and must say so explicitly, or the
    model treats the report as a flow to continue and re-registers the rule that
    just fired.
    """
    label = run.get("label") or "event task"
    action = run.get("action") or ""
    word = status_word_for(status, timed_out=timed_out)
    once = _is_once(run)

    if timed_out:
        # A timeout only exists for a one-shot: it is the deadline on the wait.
        body = (
            f"Result:\n{result_text}\n\n"
            "Tell the user it timed out and what you do know; do NOT assume the "
            "outcome happened. If it is worth waiting longer, register a new "
            "one-shot task with a larger timeout_minutes."
        )
    elif status == "completed" and once:
        body = (
            f"Result:\n{result_text}\n\n"
            "Continue the original flow from here: carry out the step that was "
            "waiting on this outcome. If the flow needs one more wait, register "
            "the NEXT one-shot task and end your turn. If it is finished, give "
            "the user the final answer. Do not re-register the task that just "
            "finished, and do not repeat work already done above."
        )
    elif status == "completed":
        body = (
            f"Result:\n{result_text}\n\n"
            "Act on it as the rule intended and tell the user what came of it. "
            "The rule stays active and will report again on its next occurrence "
            "— do NOT re-register it, and do not register a standing automation "
            "from this turn."
        )
    elif once:
        body = (
            f"Failure:\n{result_text}\n\n"
            "Report the failure to the user and propose a next step. Do not "
            "silently retry more than once."
        )
    else:
        body = (
            f"Failure:\n{result_text}\n\n"
            "Report the failure to the user and propose a next step. The rule "
            "stays active and will try again on its next occurrence — do NOT "
            "re-register it, and do not silently retry more than once."
        )

    if once:
        return (
            f"Awaited: {label}\n"
            f"Task action: {action}\n"
            f"Status: {word}\n\n"
            f"{body}"
        )
    return (
        f"Automation: {label}\n"
        f"Rule action: {action}\n"
        f"Status: {word}\n\n"
        f"{body}"
    )


def build_task_result_messages(
    run: Dict[str, Any],
    *,
    status: str,
    result_text: str,
    timed_out: bool = False,
    room: bool = False,
    dropped: Optional[List[Dict[str, Any]]] = None,
) -> tuple[str, Dict[str, Any]]:
    """Build ``(query, trigger_event)`` for a SINGLE result's continuation turn.

    Pure, so the wording is unit-testable. ``query`` is what the model reads;
    ``trigger_event`` renders the structured bubble the user sees (and marks the
    turn as event-triggered, which is what lets the agent chain one more
    one-shot task but not a standing automation).
    """
    label = run.get("label") or "event task"
    action = run.get("action") or ""
    depth = int((run.get("trigger_payload") or {}).get("task_chain_depth") or 0)
    word = status_word_for(status, timed_out=timed_out)
    once = _is_once(run)

    if once:
        header = (
            "[Event task result] The one-shot task registered earlier in this "
            "conversation has finished. This turn continues the flow that was "
            "waiting on it; the full conversation history is above.\n"
        )
    else:
        header = (
            f"[Event result] Your automation '{label}' "
            f"({_SOURCE_KIND_WORDS.get(run.get('source_kind') or '', 'rule')}) "
            f"fired at {_fired_at_for(run)} and has finished. This turn is that "
            "rule reporting back; the full conversation history is above.\n"
        )

    query = header + build_result_block(
        run, status=status, result_text=result_text, timed_out=timed_out,
    )
    if room:
        query += _room_line()
    query += _dropped_line(dropped or [])

    trigger_event = {
        "kind": "event_task_result",
        "event_type": (
            f"event task {word}: {label}" if once else f"automation {word}: {label}"
        ),
        "action": action,
        "content": result_text,
        "status": word,
        "source_kind": run.get("source_kind"),
        "subscription_id": run.get("subscription_id"),
        "event_run_id": run.get("id"),
        "label": label,
        "once": once,
        "fired_at": _fired_at_for(run),
        "dropped": len(dropped or []),
        # A one-shot continuation is one more hop in a wait chain and carries the
        # runaway counter forward. A standing rule's report is not a hop at all —
        # it is paced by the outside world, so it starts a fresh flow at 0.
        "task_chain_depth": depth + 1 if once else 0,
    }
    return query, trigger_event


def build_multi_result_messages(
    items: List[Dict[str, Any]],
    *,
    room: bool = False,
    dropped: Optional[List[Dict[str, Any]]] = None,
) -> tuple[str, Dict[str, Any]]:
    """Build ``(query, trigger_event)`` for N results arriving as ONE turn.

    ``items`` are ``{"run", "status", "result_text", "timed_out"}`` dicts in the
    order the runs finished. Coalescing matters because one request can spawn
    several one-shot tasks ("watch both pipelines, then tell me") and because a
    standing rule can fire several times while a turn runs: N separate turns
    would make the agent reconcile them blind, one at a time, and would send N
    messages to a platform user.

    ``subscription_id``/``event_run_id`` are empty for the multi shape — no
    single value is honest — and the ids move to ``event_run_ids``. Verified
    safe: the trigger's consumers (the bubble renderer, the chain-depth unpack,
    notifications, the UI) read none of those three fields.
    """
    blocks = [
        build_result_block(
            it["run"], status=it["status"], result_text=it["result_text"],
            timed_out=bool(it.get("timed_out")),
        )
        for it in items
    ]
    n = len(items)
    once_flags = [_is_once(it["run"]) for it in items]
    all_once = all(once_flags)
    n_once = sum(1 for flag in once_flags if flag)

    if all_once:
        header = (
            f"[Event task results] {n} one-shot tasks registered earlier in this "
            "conversation have finished. This turn continues the flow that was "
            "waiting on them; the full conversation history is above.\n\n"
            "Results in the order they finished:\n\n"
        )
        tail = (
            "\n\nReconcile all of the above before you act, then continue the "
            "original flow. If the flow needs one more wait, register the NEXT "
            "one-shot task and end your turn. Do not re-register any task that "
            "just finished."
        )
    else:
        mix = (
            f" ({n_once} one-shot, {n - n_once} recurring)" if n_once else ""
        )
        header = (
            f"[Event results] {n} automation results for this conversation are "
            f"ready{mix}. This turn is those rules reporting back; the full "
            "conversation history is above.\n\n"
            "Results in the order they finished:\n\n"
        )
        tail = (
            "\n\nHandle each result as its block instructs, then answer once. A "
            "one-shot result continues the flow that was waiting on it; a "
            "standing rule's result is acted on and reported. NEVER re-register "
            "a rule that just reported, and do not register a standing "
            "automation from this turn."
        )

    query = header + "\n\n---\n\n".join(blocks) + tail
    if room:
        query += _room_line()
    query += _dropped_line(dropped or [])

    words = {
        status_word_for(it["status"], timed_out=bool(it.get("timed_out")))
        for it in items
    }
    kinds = {it["run"].get("source_kind") for it in items}
    # max, not sum: depth measures how long this flow's wait chain is — and only
    # a one-shot is a hop in one, so a batch of standing reports starts at 0.
    once_depths = [
        int((it["run"].get("trigger_payload") or {}).get("task_chain_depth") or 0)
        for it, flag in zip(items, once_flags) if flag
    ]
    trigger_event = {
        "kind": "event_task_result",
        "event_type": (
            f"event task results: {n} finished" if all_once
            else f"automation results: {n} ready"
        ),
        # N actions cannot be summarised into one; the blocks carry them.
        "action": "",
        "content": "\n\n---\n\n".join(
            f"{it['run'].get('label') or 'event task'}: {it['result_text']}"
            for it in items
        ),
        "status": words.pop() if len(words) == 1 else "mixed",
        "source_kind": kinds.pop() if len(kinds) == 1 else "multiple",
        "subscription_id": "",
        "event_run_id": "",
        "event_run_ids": [it["run"].get("id") for it in items],
        "label": f"{n} automation results",
        "once": all_once,
        "fired_at": _fired_at_for(items[-1]["run"]) if items else "",
        "dropped": len(dropped or []),
        "task_chain_depth": max(once_depths) + 1 if once_depths else 0,
    }
    return query, trigger_event


def build_read_result_text(
    items: List[Dict[str, Any]],
    *,
    room: bool = False,
    dropped: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render pulled results for the tool that read them mid-turn."""
    blocks = [
        build_result_block(
            it["run"], status=it["status"], result_text=it["result_text"],
            timed_out=bool(it.get("timed_out")),
        )
        for it in items
    ]
    header = f"[Event task results — {len(items)} ready]"
    tail = (
        "\n\n--- end of results ---\n"
        "These results have been handed over and will NOT arrive again as a "
        "separate turn. A one-shot task's result continues the flow that was "
        "waiting on it; a standing rule's result is acted on and reported, and "
        "that rule is still active — never re-register one that just reported. "
        "Summarize what matters in your reply — this tool output may not be "
        "visible in later turns."
    )
    text = f"{header}\n\n" + "\n\n---\n\n".join(blocks) + tail
    if room:
        text += _room_line()
    return text + _dropped_line(dropped or [])


# ── delivery ────────────────────────────────────────────────────────────────


async def on_run_terminal(
    *,
    event_run_id: str,
    profile: str,
    status: str,
    final_text: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    """Hand a finished run's result back to the conversation that registered it.

    Safe to call for every terminal event run — a run with nowhere to report
    returns :data:`NOT_TASK` after one cheap read. Never raises: a delivery
    problem must not turn a completed run into a failed one.
    """
    from app.storage import get_event_run_storage

    store = get_event_run_storage()
    try:
        run = await store.get(event_run_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] failed to load run {event_run_id}")
        return FAILED
    if not run or not run.get("deliver_to_origin"):
        return NOT_TASK
    if run.get("origin_delivered_at"):
        # Someone already holds this row's lock — a sibling flush that coalesced
        # it, a stale close-out, or a restore. Nothing left to do, and no reason
        # to read an inbox we would lose every claim in.
        return ALREADY_DELIVERED

    once = _is_once(run)
    source_kind = run.get("source_kind") or ""
    subscription_id = run.get("subscription_id") or ""

    # ── quiet close-outs (claim, but never surface) ────────────────────────
    # A cancelled run is a deliberate kill from the Events page. Injecting "your
    # task was cancelled" into the chat would be noise on top of an action the
    # user just took there, so terminate the rule quietly instead. Claiming here
    # (rather than in the inbox) is also what keeps such rows out of
    # ``list_pending_for_origin`` and unpinned from the retention prune.
    if status == "cancelled":
        if not await _close_out(store, event_run_id):
            return ALREADY_DELIVERED
        if once:
            _terminate_task_subscription(source_kind, subscription_id, "cancelled")
        return SKIPPED_CANCELLED

    origin_id = run.get("origin_conversation_id")
    if not origin_id:
        # The origin conversation was deleted (FK SET NULL). Nothing to continue
        # — leave the run's own notification in place so the outcome is not lost
        # entirely.
        if not await _close_out(store, event_run_id):
            return ALREADY_DELIVERED
        if once:
            _terminate_task_subscription(source_kind, subscription_id, "completed")
        return ORIGIN_GONE

    # ── the fork ───────────────────────────────────────────────────────────
    # ONE synchronous call decides and parks, so nothing can interleave between
    # the liveness check and the park (see task_result_inbox for why that is
    # load-bearing). Parking takes NO claim and writes nothing: this row is
    # already a pending inbox entry by virtue of being terminal.
    notice = {
        "event_run_id": event_run_id,
        "label": run.get("label") or "event task",
        "status_word": status_word_for(
            status, timed_out=bool((run.get("trigger_payload") or {}).get("timed_out")),
        ),
        # Lets the notice tell a result the flow is BLOCKED on from a routine
        # report, so the agent knows which one is worth reading right now.
        "once": once,
    }
    if task_result_inbox.park_if_bound(origin_id, notice):
        _publish_changed(profile, source_kind)
        logger.info(
            f"[event_task] parked {source_kind} task result (run={event_run_id}, "
            f"status={status}) — conversation {origin_id} is mid-turn"
        )
        return PARKED

    # ── idle: deliver now, sweeping up any sibling still waiting ───────────
    try:
        result = await flush_origin_inbox(
            conversation_id=origin_id, profile=profile, reason="idle",
            final_text_hint={event_run_id: (final_text or error or "")},
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] delivery failed for run {event_run_id}")
        return FAILED

    if result.origin_gone:
        return ORIGIN_GONE
    if event_run_id in result.claimed:
        logger.info(
            f"[event_task] delivered {source_kind} result "
            f"(run={event_run_id}, status={status}, once={once}) to "
            f"conversation {origin_id}"
        )
        return DELIVERED
    if event_run_id in result.dropped:
        # Closed out rather than reported (too old, or past this conversation's
        # per-delivery cap). The origin will never speak for it, so the hidden
        # run's own notification must stay — see SKIPPED_STALE.
        logger.info(
            f"[event_task] closed out {source_kind} result without reporting "
            f"(run={event_run_id}, status={status}) for conversation {origin_id}"
        )
        return SKIPPED_STALE
    # This call did not claim our row, so a sibling flush already delivered it
    # — the boot-sweep coalescing case, where injecting row 1 sweeps up rows
    # 2..N. Reporting FAILED here would release a claim we do not hold and
    # re-deliver the result on every boot, forever.
    return ALREADY_DELIVERED


async def flush_origin_inbox(
    *,
    conversation_id: str,
    profile: str,
    reason: str = "turn_end",
    final_text_hint: Optional[Dict[str, str]] = None,
) -> FlushOutcome:
    """Report every result waiting on this conversation as ONE turn.

    The only injection path in the system — the idle delivery, the turn-end
    reconciliation and (transitively) the boot sweep all land here, so there is
    exactly one place that words, bounds, forwards and enqueues a continuation.

    Volume is bounded here too: only the newest ``max_results_per_delivery``
    STANDING results are reported, and only those younger than the age bound.
    The rest are closed out through the same exactly-once claim (so they can
    never be reported later, and stop pinning the retention prune) and the turn
    names them, because a result silently vanishing is worse than a long turn.
    """
    from app.events import queue as event_queue
    from app.events import runner as event_runner
    from app.events.run_config import max_results_per_delivery
    from app.events.user_message_delivery import is_room_conversation
    from app.storage import get_event_run_storage

    store = get_event_run_storage()
    conversation_storage = event_runner.get_conversation_storage()
    if conversation_storage is None:
        logger.error("[event_task] conversation storage not initialized; cannot deliver")
        return FlushOutcome(set())

    try:
        rows = await store.list_pending_for_origin(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] could not read inbox for {conversation_id}")
        return FlushOutcome(set())
    if not rows:
        return FlushOutcome(set())

    conv = await conversation_storage.get_conversation(conversation_id)
    gone = conv is None or conv.get("kind") == "event_run"
    if gone:
        # Nothing to continue and nothing to retry: claim and close out, so the
        # boot sweep does not pick these up forever (which would also pin them
        # against the run-history prune).
        logger.warning(
            f"[event_task] origin {conversation_id} is gone or is itself an event "
            f"run; closing out {len(rows)} pending result(s)"
        )
        for row in rows:
            if await _close_out(store, row["id"]) and _is_once(row):
                _terminate_task_subscription(
                    row.get("source_kind") or "", row.get("subscription_id") or "",
                    "completed",
                )
        return FlushOutcome(set(), origin_gone=True)

    keep, over = _partition_pending(
        rows, cap=max_results_per_delivery(), cutoff_ms=_stale_cutoff_ms(),
    )
    dropped_items: List[Dict[str, Any]] = []
    dropped_ids: Set[str] = set()
    for row in over:
        if not await _close_out(store, row["id"]):
            continue  # a concurrent read/flush owns this one
        dropped_ids.add(row["id"])
        dropped_items.append(await _describe_row(row))
        if _is_once(row):
            _terminate_task_subscription(
                row.get("source_kind") or "", row.get("subscription_id") or "", "completed",
            )
    if dropped_ids:
        logger.warning(
            f"[event_task] closed out {len(dropped_ids)} stale/over-cap result(s) "
            f"for {conversation_id} without reporting them"
        )

    items: List[Dict[str, Any]] = []
    claimed: Set[str] = set()
    for row in keep:
        if not await store.claim_delivery(row["id"]):
            continue  # a concurrent read/flush owns this one
        claimed.add(row["id"])
        items.append(await _describe_row(row, hint=(final_text_hint or {}).get(row["id"])))

    if not items:
        # Everything owed was dropped. The hidden runs' own notifications were
        # suppressed when they parked, so without this the outcome would vanish
        # from every surface except the Events page.
        if dropped_items:
            _notify_dropped(profile, conversation_id, dropped_items)
        return FlushOutcome(set(), dropped=frozenset(dropped_ids))

    is_room = is_room_conversation(conv)
    adapter = None
    try:
        history_messages = await _load_history(conversation_storage, conversation_id, profile)

        # Mirror the continuation to the platform when the origin chat lives on
        # an external channel. The runs' own output is deliberately NOT
        # forwarded (see run_dispatcher), and N results coalesce into this one
        # turn, so the user gets exactly one message.
        try:
            from app.events.run_dispatcher import _maybe_forward_to_channel
            adapter = await _maybe_forward_to_channel(
                conversation_storage, conversation_id, conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[event_task] channel forwarder setup failed")

        if len(items) == 1:
            it = items[0]
            query, trigger_event = build_task_result_messages(
                it["run"], status=it["status"], result_text=it["result_text"],
                timed_out=bool(it.get("timed_out")),
                room=is_room, dropped=dropped_items,
            )
        else:
            query, trigger_event = build_multi_result_messages(
                items, room=is_room, dropped=dropped_items,
            )

        metadata = {
            "source": "event_task_result",
            "status": trigger_event["status"],
            "source_kind": trigger_event.get("source_kind"),
            "subscription_id": trigger_event.get("subscription_id"),
            "event_run_id": trigger_event.get("event_run_id"),
            "event_run_ids": [it["run"].get("id") for it in items],
        }
        # The TRIGGER bubble gets its own dict. The agent's answer is persisted
        # with ``metadata`` too, so anything that must recognise the machine-
        # written block (the UI badge, the CLI transcript, the channel forwarder,
        # the room relevance judge) keys on ``trigger`` — keying on ``source``
        # alone would tag the agent's own reply as an automation result.
        trigger_metadata = {
            **metadata,
            "trigger": True,
            "label": trigger_event.get("label"),
            "once": trigger_event.get("once"),
            "fired_at": trigger_event.get("fired_at"),
            "dropped": trigger_event.get("dropped"),
        }

        from app.agent.stream_runner import make_run_id

        await event_queue.enqueue_user_message(
            conversation_id=conversation_id,
            run_id=make_run_id(conversation_id, kind="event"),
            profile=profile,
            query=query,
            history_messages=history_messages,
            reasoning=True,
            user_message_metadata=trigger_metadata,
            agent_message_metadata=metadata,
            push_user_message=False,
            trigger_event=trigger_event,
            update_title_from_query=False,
            # A room's answer IS the notification: it is posted where everyone
            # can see it, and a push deep-linked at a hidden seat is unopenable.
            publish_notification=not is_room,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_task] injection failed for {conversation_id}; releasing "
            f"{len(claimed)} claim(s) so a later flush retries"
        )
        for run_id_pk in claimed:
            try:
                await store.clear_delivery_claim(run_id_pk)
            except Exception:  # noqa: BLE001
                logger.exception(f"[event_task] failed to release claim on {run_id_pk}")
        # The forwarder was armed for a run that will now never start. Left
        # standing it absorbs whatever runs next on this conversation — pushing
        # to the platform an answer the user asked for in the web UI.
        if adapter is not None:
            try:
                await adapter.release_external_run(conversation_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[event_task] failed to release the forwarder for {conversation_id}"
                )
        return FlushOutcome(set(), dropped=frozenset(dropped_ids))

    for it in items:
        await _finish_delivery(store, it["run"], profile, MODE_INJECTED)
    _publish_all_changed(profile, items)
    logger.info(
        f"[event_task] reported {len(items)} result(s) into {conversation_id} "
        f"({reason}"
        + (f", dropped {len(dropped_ids)}" if dropped_ids else "")
        + ")"
    )
    return FlushOutcome(claimed, dropped=frozenset(dropped_ids))


async def read_origin_inbox(
    *, conversation_id: str, profile: str,
) -> tuple[str, List[int]]:
    """Hand every waiting result to the agent that asked for them mid-turn.

    Returns ``(text, chain_depths)``. Composes each result's text BEFORE
    claiming it: a claim is irreversible for anyone else, so taking it first
    would mean a failure between claim and hand-over destroys the result. And
    because "Stop" arrives as :class:`asyncio.CancelledError` — which the leaf
    runner does not catch — the whole body releases its own claims on any
    ``BaseException`` before re-raising.

    Applies the same volume bounds as the flush: a rule that fired fifty times
    during a long turn must not hand the agent fifty blocks.
    """
    from app.events import runner as event_runner
    from app.events.run_config import max_results_per_delivery
    from app.events.user_message_delivery import is_room_conversation
    from app.storage import get_event_run_storage

    store = get_event_run_storage()
    try:
        rows = await store.list_pending_for_origin(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] could not read inbox for {conversation_id}")
        return (
            "Could not read the task-result inbox just now. It will be delivered "
            "as a new turn when this turn ends.",
            [],
        )
    if not rows:
        return (
            "No task results are waiting. They were already delivered as a turn, "
            "already read, or the run was removed — disregard any earlier notice "
            "and carry on with what you were doing.",
            [],
        )

    # Close out what must not be reported BEFORE anything is claimed for the
    # hand-over: dropped ids must never enter ``claimed``, or the release below
    # would put a closed-out result back into the inbox.
    keep, over = _partition_pending(
        rows, cap=max_results_per_delivery(), cutoff_ms=_stale_cutoff_ms(),
    )
    dropped_items: List[Dict[str, Any]] = []
    for row in over:
        if not await _close_out(store, row["id"]):
            continue
        dropped_items.append(await _describe_row(row))
        if _is_once(row):
            _terminate_task_subscription(
                row.get("source_kind") or "", row.get("subscription_id") or "", "completed",
            )

    is_room = False
    try:
        conversation_storage = event_runner.get_conversation_storage()
        if conversation_storage is not None:
            is_room = is_room_conversation(
                await conversation_storage.get_conversation(conversation_id)
            )
    except Exception:  # noqa: BLE001
        logger.debug("[event_task] could not classify the origin", exc_info=True)

    items: List[Dict[str, Any]] = []
    claimed: List[str] = []
    try:
        for row in keep:
            described = await _describe_row(row)
            if not await store.claim_delivery(row["id"]):
                continue  # a concurrent flush owns this one; it will be injected
            claimed.append(row["id"])
            items.append(described)

        if not items:
            if dropped_items:
                return (
                    "No results are waiting any more."
                    + _dropped_line(dropped_items).lstrip("\n"),
                    [],
                )
            return (
                "No task results are waiting — they are being delivered as a new "
                "turn right now. Carry on; you will see them shortly.",
                [],
            )

        text = build_read_result_text(items, room=is_room, dropped=dropped_items)
    except BaseException:
        # Includes CancelledError (the user pressed Stop). Release anything this
        # call claimed, so the results are re-deliverable instead of lost.
        for run_id_pk in claimed:
            try:
                await store.clear_delivery_claim(run_id_pk)
            except Exception:  # noqa: BLE001
                logger.exception(f"[event_task] failed to release claim on {run_id_pk}")
        raise

    depths: List[int] = []
    for it in items:
        await _finish_delivery(store, it["run"], profile, MODE_READ)
        depths.append(
            int((it["run"].get("trigger_payload") or {}).get("task_chain_depth") or 0)
        )
    _publish_all_changed(profile, items)
    logger.info(
        f"[event_task] agent read {len(items)} task result(s) in {conversation_id}"
    )
    return text, depths


def _publish_all_changed(profile: str, items: List[Dict[str, Any]]) -> None:
    """Nudge the Events page once per source_kind present in a batch."""
    for kind in {(it["run"].get("source_kind") or "") for it in items}:
        _publish_changed(profile, kind)


async def _describe_row(
    row: Dict[str, Any], *, hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a pending row's result text into a renderable item."""
    timed_out = bool((row.get("trigger_payload") or {}).get("timed_out"))
    text = (hint or "").strip()
    if not text:
        text = ((await _recover_final_text(row)) or "").strip()
    if not text:
        text = (row.get("error") or "").strip() or (
            row.get("pending_question") or ""
        ).strip()
    return {
        "run": row,
        "status": row.get("status") or "failed",
        "result_text": text or _NO_OUTPUT,
        "timed_out": timed_out,
    }


async def _finish_delivery(
    store: Any, run: Dict[str, Any], profile: str, mode: str,
) -> None:
    """Close out one delivered result: mode, subscription, admin nudge.

    Per-row on purpose: one coalesced flush can span a skill-event row and a
    file-watcher row, and each one's subscription has to be terminated in its
    own table or that rule stays ``triggered`` forever.

    Only a ONE-SHOT terminates — a standing rule must stay armed to report
    again. The SQL already guarantees it (``set_task_status`` matches
    ``task = TRUE AND task_status = 'triggered'``, which a standing row never
    satisfies), but saying it here keeps the intent readable.
    """
    await _set_mode(store, run.get("id") or "", mode)
    if _is_once(run):
        _terminate_task_subscription(
            run.get("source_kind") or "", run.get("subscription_id") or "", "completed",
        )


async def _set_mode(store: Any, run_id_pk: str, mode: str) -> None:
    if not run_id_pk:
        return
    try:
        await store.set_delivery_mode(run_id_pk, mode)
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] failed to record delivery mode for {run_id_pk}")


async def _close_out(store: Any, run_id_pk: str) -> bool:
    """Take one result out of the inbox without reporting it. Never raises."""
    if not run_id_pk:
        return False
    try:
        return bool(await store.close_out_delivery(run_id_pk, MODE_SKIPPED))
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] failed to close out {run_id_pk}")
        return False


def _fired_at_for(run: Dict[str, Any]) -> str:
    """When the trigger happened, in the profile's own timezone."""
    payload = run.get("trigger_payload") or {}
    for key in ("fired_at", "detected_at"):
        value = payload.get(key)
        if value:
            return str(value)
    created = run.get("created_at")
    if created:
        try:
            return _format_epoch(float(created) / 1000, run.get("profile") or "")
        except (TypeError, ValueError):
            pass
    return "an unknown time"


def _notify_dropped(
    profile: str, conversation_id: str, items: List[Dict[str, Any]],
) -> None:
    """Say something when a whole batch was closed out unreported.

    The hidden runs' own notifications were suppressed when their results
    parked, on the promise that the conversation would speak for them. If every
    one is then dropped, this is the only thing that keeps the promise.
    """
    if not items:
        return
    names = ", ".join(
        it["run"].get("label") or "automation" for it in items[:5]
    )
    more = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
    try:
        from app.events import get_event_notifications

        get_event_notifications().push(
            profile=profile,
            conversation_id=conversation_id,
            conversation_title="",
            message_preview=(
                f"{len(items)} automation result(s) were dropped without being "
                f"reported (too old, or past the per-conversation limit): {names}{more}"
            ),
            kind="error",
            priority="normal",
        )
    except Exception:  # noqa: BLE001
        logger.exception("[event_task] failed to notify about dropped results")


async def _load_history(conversation_storage: Any, conversation_id: str, profile: str) -> list:
    """Load the origin conversation's history, exactly like a web POST does."""
    try:
        from app.config.user_config import replay_reasoning_enabled
        from app.utils.common import convert_db_messages_to_history

        db_msgs = await conversation_storage.get_messages(conversation_id)
        if db_msgs:
            return convert_db_messages_to_history(
                db_msgs, include_reasoning=replay_reasoning_enabled(profile),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_task] failed to load history for {conversation_id}; "
            "continuing with an empty history"
        )
    return []


# ── timeout ─────────────────────────────────────────────────────────────────


async def deliver_timeout(source_kind: str, sub: Dict[str, Any]) -> str:
    """Report an expired task back to its origin conversation.

    The caller has already claimed the subscription (``active -> timed_out``),
    so this runs at most once per task. A terminal ``event_runs`` row is written
    with no conversation of its own: the timeout produced no agent run, but the
    row makes the outcome visible on the Events page and — more importantly —
    lets the delivery ride the same crash-safe claim/sweep path as a real run.
    """
    from app.storage import get_event_run_storage

    profile = sub.get("profile") or ""
    origin_id = sub.get("conversation_id")
    label = _subscription_label(source_kind, sub)
    deadline = _format_epoch(sub.get("timeout_at"), profile)
    message = (
        f"The awaited event never fired before the deadline ({deadline}), so "
        "the task action never ran."
    )
    try:
        created = await get_event_run_storage().create(
            profile=profile,
            source_kind=source_kind,
            subscription_id=sub["id"],
            conversation_id=None,
            label=label,
            action=sub.get("action") or "",
            # A timeout only exists for a one-shot: it is the deadline on the
            # wait, so the result must render with one-shot wording.
            trigger_payload={
                "timed_out": True, "timeout_at": sub.get("timeout_at"), "once": True,
            },
            origin_conversation_id=origin_id,
            deliver_to_origin=bool(origin_id),
            status="failed",
            error=message,
            finished=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] failed to record timeout run for {sub.get('id')}")
        return FAILED

    _publish_changed(profile, source_kind)
    return await on_run_terminal(
        event_run_id=created["run"]["id"],
        profile=profile,
        status="failed",
        final_text=message,
        error=message,
    )


# ── boot recovery ───────────────────────────────────────────────────────────


async def sweep_undelivered() -> int:
    """Report results a crash left stranded. Idempotent; runs at boot.

    Must run AFTER :meth:`EventRunStorage.recover_after_restart`, which flips
    interrupted ``running`` rows to ``failed`` — that is what makes them
    terminal, and therefore deliverable, here — and AFTER the channel adapters
    start, or a recovered result bound to a Telegram chat or a platform group
    reaches the web UI only (its forwarder needs a live adapter).

    "Stranded by a crash" and "parked while the origin was mid-turn" are the
    same query by construction, so this also picks up results the process died
    holding. Note the interaction with coalescing: reporting row 1 claims every
    sibling of the same origin, so rows 2..N must resolve to
    :data:`ALREADY_DELIVERED` — never :data:`FAILED`, whose claim release would
    resurrect an already-reported result and re-deliver it on every boot.
    :func:`on_run_terminal` guarantees that by keying on the id set the flush
    reports back.

    A standing rule's result that is simply too old is closed out here rather
    than reported: after a week of downtime, seven "here is today's news" turns
    arriving at once — in a chat, a room, or a platform group — are noise.
    One-shot results are never dropped, however old.
    """
    from app.storage import get_event_run_storage

    store = get_event_run_storage()
    try:
        rows = await store.list_undelivered_task_runs()
    except Exception:  # noqa: BLE001
        logger.exception("[event_task] boot sweep: failed to list undelivered runs")
        return 0

    cutoff = _stale_cutoff_ms()
    delivered = 0
    stale: Dict[str, List[Dict[str, Any]]] = {}
    for run in rows:
        try:
            # A cancelled row still goes through on_run_terminal: it terminates
            # its rule as "cancelled", which the close-out below cannot do.
            if (
                cutoff is not None
                and run.get("status") != "cancelled"
                and not _is_once(run)
                and _terminal_ts(run) < cutoff
            ):
                if await _close_out(store, run["id"]):
                    stale.setdefault(
                        run.get("origin_conversation_id") or "", [],
                    ).append(await _describe_row(run))
                continue

            final_text = await _recover_final_text(run)
            outcome = await on_run_terminal(
                event_run_id=run["id"],
                profile=run.get("profile") or "",
                status=run.get("status") or "failed",
                final_text=final_text,
                error=run.get("error"),
            )
            if outcome == DELIVERED:
                delivered += 1
        except Exception:  # noqa: BLE001
            logger.exception(f"[event_task] boot sweep: delivery failed for {run.get('id')}")
    if delivered:
        logger.info(f"[event_task] boot sweep reported {delivered} stranded result(s)")
    for origin_id, items in stale.items():
        logger.info(
            f"[event_task] boot sweep closed out {len(items)} stale result(s) "
            f"owed to {origin_id or '(no origin)'}"
        )
        if origin_id:
            _notify_dropped(items[0]["run"].get("profile") or "", origin_id, items)

    await _reconcile_orphaned_tasks()
    return delivered


async def _recover_final_text(run: Dict[str, Any]) -> Optional[str]:
    """Recover a completed run's answer from its conversation (nothing in memory)."""
    if run.get("status") != "completed" or not run.get("conversation_id"):
        return run.get("error")
    try:
        from app.events import runner as event_runner

        conversation_storage = event_runner.get_conversation_storage()
        if conversation_storage is None:
            return run.get("error")
        msgs = await conversation_storage.get_messages(run["conversation_id"])
        for msg in reversed(msgs or []):
            if msg.get("role") != "agent":
                continue
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            # An event run persists its own TRIGGER as an agent message too. If
            # the final answer failed to persist (that write is best-effort),
            # the newest agent message is that trigger — echoing it back as "the
            # result" would be actively misleading, so skip it.
            if content.startswith("Trigger:"):
                continue
            return msg["content"]
    except Exception:  # noqa: BLE001
        logger.exception(f"[event_task] could not recover final text for run {run.get('id')}")
    return run.get("error")


async def _reconcile_orphaned_tasks() -> None:
    """Re-arm or close tasks stuck mid-claim by a crash.

    A task claimed at fire time but crashed before its run row existed would sit
    at ``triggered`` forever with nothing to deliver. With no live and no
    undeliverable run behind it, the honest state is "spent".
    """
    from app.storage import (
        get_event_run_storage, get_event_subscription_storage,
        get_file_watcher_storage,
    )

    run_store = get_event_run_storage()
    for source_kind, storage in (
        ("skill_event", get_event_subscription_storage()),
        ("file_watcher", get_file_watcher_storage()),
    ):
        try:
            rows = [r for r in storage.list_all() if r.get("task_status") == "triggered"]
        except Exception:  # noqa: BLE001
            logger.exception(f"[event_task] reconcile: failed to list {source_kind} tasks")
            continue
        for sub in rows:
            try:
                if await run_store.has_live_run_for_subscription(source_kind, sub["id"]):
                    continue
                storage.set_task_status(sub["id"], "completed")
                logger.info(
                    f"[event_task] reconciled orphaned {source_kind} task {sub['id']} "
                    "(claimed but no run survived) → completed"
                )
            except Exception:  # noqa: BLE001
                logger.exception(f"[event_task] reconcile failed for {sub.get('id')}")


# ── small helpers ───────────────────────────────────────────────────────────


def _task_storage(source_kind: str) -> Any:
    from app.storage import get_event_subscription_storage, get_file_watcher_storage

    if source_kind == "skill_event":
        return get_event_subscription_storage()
    if source_kind == "file_watcher":
        return get_file_watcher_storage()
    return None  # schedules terminate inside ScheduleManager._fire


def _terminate_task_subscription(source_kind: str, subscription_id: str, status: str) -> None:
    storage = _task_storage(source_kind)
    if storage is None or not subscription_id:
        return
    try:
        storage.set_task_status(subscription_id, status)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[event_task] failed to mark {source_kind} task {subscription_id} {status}"
        )


def _subscription_label(source_kind: str, sub: Dict[str, Any]) -> str:
    if source_kind == "skill_event":
        skill = sub.get("skill_name") or ""
        event_type = sub.get("event_type") or ""
        return f"{skill}:{event_type}" if skill else event_type
    return sub.get("name") or "File watcher"


def _format_epoch(epoch: Any, profile: str) -> str:
    if not epoch:
        return "unknown"
    try:
        from datetime import datetime
        from app.config.timezone import resolve_tzinfo

        return datetime.fromtimestamp(float(epoch), resolve_tzinfo(profile)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def _publish_changed(profile: str, source_kind: str) -> None:
    """Nudge the Events page: the run list and the rule's own section changed."""
    try:
        from app.events.event_runs_admin_bus import publish_event_runs_changed
        publish_event_runs_changed(profile)
    except Exception:  # noqa: BLE001
        logger.debug("[event_task] event-runs admin nudge failed", exc_info=True)
    # Published straight on the bus (not via the app.api helpers) so this module
    # stays free of an app.events → app.api import edge. Payload-less by design:
    # subscribers rebuild their own snapshot on the tick.
    try:
        if source_kind == "skill_event":
            from app.events.skill_events_admin_bus import get_skill_events_admin_stream_bus
            get_skill_events_admin_stream_bus().publish(profile, {})
        elif source_kind == "file_watcher":
            from app.events.file_watcher_admin_bus import get_file_watcher_admin_stream_bus
            get_file_watcher_admin_stream_bus().publish(profile, {})
    except Exception:  # noqa: BLE001
        logger.debug("[event_task] rule admin nudge failed", exc_info=True)
