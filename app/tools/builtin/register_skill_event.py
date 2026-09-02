"""Skill-event subscription helpers.

Records a (conversation, skill, event_type, action) tuple so that whenever a
new ``*.md`` file appears in ``<skill_dir>/events/<event_type>/`` (produced by
the skill's own listener daemon), the reasoning agent re-runs ``action`` with
the file content appended — and streams the result into the conversation.

This used to be a built-in *tool* (``register_skill_event``) the model invoked
via a separate, active-skill-pinned schema. Event subscription now lives on each
skill's own tool schema (a ``subscribe`` object carrying that skill's event
enum), so the reasoning agent calls :func:`register_skill_events` directly with
the target skill pinned by its own ``tool_id``/source dir — no active-skill
state, no separate tool. The resolver/metadata helpers here are still imported
by :mod:`app.api.events` and :mod:`app.events.manager`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.storage import get_event_subscription_storage
from app.utils.skill_source import lookup_skill_source
from app.tools.ids import slugify
from app.utils.logger import logger


def _resolve_skill(skill_name: str, profile: str) -> Optional[tuple[str, str]]:
    """Resolve a user-supplied skill name to ``(tool_id, source_dir)``.

    Skill rows are keyed by ``<profile>__<slug>`` (see
    :mod:`app.tools.registry`). For resilience we also accept a bare slug or the
    original SKILL.md ``name`` value (e.g. ``imap-email``); a leading
    ``<profile>__`` is stripped before re-slugging so a stale prefix on a
    different profile still resolves.
    """
    raw = (skill_name or "").strip()
    if not raw or not profile:
        return None
    prefix = f"{profile}__"
    bare = raw[len(prefix):] if raw.startswith(prefix) else raw
    candidates = [
        f"{profile}__{slugify(bare)}",
        slugify(bare),
        raw,
    ]
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        source = lookup_skill_source(cand, profile)
        if source:
            return cand, source
    return None


def _resolve_skill_source(skill_name: str, profile: str) -> Optional[str]:
    """Convenience: source dir only (used by callers that don't need the id)."""
    resolved = _resolve_skill(skill_name, profile)
    return resolved[1] if resolved else None


def _normalize_triggers(raw: Any) -> List[str]:
    """Coerce a trigger argument into a deduplicated list of trimmed names.

    Accepts the canonical array shape and, for resilience, a bare string —
    LLMs occasionally revert to the legacy single-trigger habit even with the
    array schema in front of them.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items: List[Any] = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _read_events_metadata(source_dir: Path) -> List[Dict[str, Any]]:
    """Return the list under ``metadata.events.event_type`` from SKILL.md."""
    skill_md = source_dir / "SKILL.md"
    if not skill_md.exists():
        return []
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return []
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return []
    end_idx = stripped.find("---", 3)
    if end_idx == -1:
        return []
    try:
        data = yaml.safe_load(stripped[3:end_idx]) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        return []
    events = metadata.get("events") or {}
    if not isinstance(events, dict):
        return []
    items = events.get("event_type") or []
    if not isinstance(items, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            cleaned.append(item)
    return cleaned


async def register_skill_events(
    *,
    profile: str,
    context_id: str,
    skill_id: str,
    skill_source: str,
    triggers: List[str],
    action: str,
    request_context: str = "",
    task: bool = False,
    timeout_minutes: Any = None,
) -> str:
    """Subscribe a conversation to one or more of a skill's declared events.

    The reasoning agent calls this directly when the model invokes a skill tool
    with a ``subscribe`` payload. ``skill_id``/``skill_source`` are pinned by the
    caller to the exact skill whose tool was invoked, so there is no active-skill
    ambiguity. Returns a human-readable confirmation (or error) string that the
    agent appends as the tool result.

    With ``task=True`` the subscription becomes a ONE-SHOT EVENT TASK: it waits
    for the next matching event, runs once, delivers the outcome back into the
    registering conversation, then terminates. ``timeout_minutes`` bounds that
    wait (see :mod:`app.events.task_policy`).
    """
    profile = (profile or "").strip()
    context_id = (context_id or "").strip()
    skill_id = (skill_id or "").strip()
    skill_source = (skill_source or "").strip()
    triggers = _normalize_triggers(triggers)
    action = (action or "").strip()

    if not profile:
        return "Internal error: profile not provided to register_skill_events."
    if not context_id:
        return "Internal error: context_id not provided to register_skill_events."
    if not skill_id:
        return (
            "Internal error: skill_id was not provided. Event subscription must "
            "be pinned to a specific skill."
        )
    if not skill_source:
        # Fall back to looking up the source from storage if only the id is known.
        looked_up = lookup_skill_source(skill_id, profile)
        if not looked_up:
            return (
                f"Skill '{skill_id}' was not found for profile '{profile}'. "
                f"Make sure the skill is installed and enabled."
            )
        skill_source = looked_up
    if not triggers:
        return "trigger is required (non-empty array of event names)."
    if not action:
        return "action is required."

    # Event-task validation before anything is created or any LLM gate runs, so
    # a malformed call costs nothing and leaves no half-registered state.
    from app.events.task_policy import format_timeout_clause, resolve_task_timeout

    task = bool(task)
    timeout_at, timeout_error = resolve_task_timeout(timeout_minutes, task=task)
    if timeout_error:
        return timeout_error
    if task and len(triggers) != 1:
        # One logical wait must not fan out into N rows: they terminate
        # independently, so the losers would still be armed after the flow moved
        # on and would eventually inject a stale result into a finished
        # conversation. Two genuine waits are two deliberate calls.
        trig_list = ", ".join(f"'{t}'" for t in triggers)
        return (
            f"`task: true` registers ONE awaited outcome, so it needs exactly "
            f"one `trigger` (you passed {len(triggers)}: {trig_list}). Nothing "
            "was registered. Re-call with the single event you are waiting for "
            "— or, if you genuinely need to wait on several independent "
            "outcomes, make one `subscribe` call per event; each becomes its own "
            "task and delivers its own result."
        )

    canonical_skill_id = skill_id
    source_dir_str = skill_source
    source_dir = Path(source_dir_str)

    events = _read_events_metadata(source_dir)
    valid_names = [e["name"] for e in events]
    if not valid_names:
        return (
            f"Skill '{canonical_skill_id}' does not declare any events in its "
            f"metadata.events. Cannot register a trigger."
        )
    invalid = [t for t in triggers if t not in valid_names]
    if invalid:
        return (
            f"trigger(s) {invalid} are not declared by skill "
            f"'{canonical_skill_id}'. Valid triggers: {', '.join(valid_names)}."
        )

    # Resolve (or create) the conversation row. On the very first user turn the
    # executor has not yet persisted a conversation row — that only happens after
    # the reasoning loop finishes (see ``app/agent/executor.py``). Creating it
    # eagerly here gives the subscription a valid FK target without waiting for
    # another turn, and the executor's later ``get_or_create_conversation`` call
    # is a no-op because we share the same ``context_id``.
    from app.storage import get_conversation_storage

    conv_storage = get_conversation_storage()
    try:
        conv = await conv_storage.get_or_create_conversation(
            profile=profile, context_id=context_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("register_skill_events: get_or_create_conversation failed")
        return f"Could not resolve the active conversation: {exc}"
    if conv is None:
        return "Could not resolve the active conversation."
    conversation_id = conv["id"]

    # Self-containment gate: the subscription's action runs later in a fresh
    # conversation with no context, so refuse to persist one that references info
    # it doesn't inline. Fail-open (no LLM / error → proceeds).
    from app.events.action_check import gate_registration_action, build_rejection_message

    check = await gate_registration_action(
        profile=profile, action=action, request_context=request_context,
        tool_name="this skill's subscribe", conversation_id=conversation_id,
        task=task,
    )
    if check is not None:
        return build_rejection_message(
            tool_name="this skill's subscribe", missing=check.missing,
            reason=check.reason, task=task,
        )

    # Persist + watch using the canonical tool_id so every entry agrees
    # regardless of the surface form the LLM happened to pass. One row + one
    # watcher per (conversation, skill, trigger). Multiple triggers in the same
    # call become independent subscriptions that share an action.
    store = get_event_subscription_storage()
    rows: List[Dict[str, Any]] = []
    for trigger in triggers:
        try:
            row = store.insert(
                conversation_id=conversation_id,
                profile=profile,
                skill_name=canonical_skill_id,
                event_type=trigger,
                action=action,
                task=task,
                timeout_at=timeout_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_skill_events: insert failed")
            return f"Failed to save subscription for trigger '{trigger}': {exc}"
        rows.append(row)

    # No per-subscription watcher arming: Cremind already monitors every
    # event-listener skill's events/ tree continuously (see app.events.manager),
    # so persisting the subscription row is all that's needed — the blanket
    # watch resolves it on fan-out. Arming a second watcher here would race the
    # blanket one to unlink/enqueue the same file.

    # Push the new subscriptions to any open events-page SSE subscribers so the
    # admin UI lights them up without a manual refresh. Imported locally to avoid
    # pulling api.events into tool-import time.
    try:
        from app.api.events import publish_skill_events_admin_changed
        publish_skill_events_admin_changed(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"register_skill_events: admin-bus publish failed: {exc}")

    if task:
        # Single trigger guaranteed above.
        confirmation = (
            f"Registered a one-shot TASK on the '{triggers[0]}' event of skill "
            f"'{canonical_skill_id}' (id {rows[0]['id']}). It waits for the next "
            f"such event{format_timeout_clause(timeout_at, timeout_minutes)}, runs "
            f"\"{action}\" once in a background conversation, reports the "
            "outcome back into THIS conversation as a new turn, then stops "
            "itself. Nothing else is needed from you about it: register any "
            "further tasks now, then END YOUR TURN with a short message telling "
            "the user exactly what you are waiting for. Do not sleep, poll, or "
            "re-check."
        )
    elif len(triggers) == 1:
        t = triggers[0]
        confirmation = (
            f"Subscribed this conversation to the '{t}' event of skill "
            f"'{canonical_skill_id}' (id {rows[0]['id']}). Each time a new event "
            f"arrives in {source_dir / 'events' / t} I'll run \"{action}\" in a "
            "background conversation and report the result back here as a new "
            "turn; the subscription stays active until the user stops it. Finish "
            "this turn with a short confirmation to the user — do not wait for "
            "the first run."
        )
    else:
        trig_list = ", ".join(f"'{t}'" for t in triggers)
        confirmation = (
            f"Subscribed this conversation to {len(triggers)} events of skill "
            f"'{canonical_skill_id}': {trig_list}. Each time any of these events "
            f"fires (under {source_dir / 'events'}/<event_type>/) I'll run "
            f"\"{action}\" in a background conversation and report the result "
            "back here as a new turn — one result per event; each subscription "
            "stays active until the user stops it. Finish this turn with a short "
            "confirmation to the user — do not wait for the first run."
        )

    logger.info(
        f"register_skill_events: conv={conversation_id} "
        f"skill={canonical_skill_id} triggers={triggers} "
        f"ids={[r['id'] for r in rows]}"
    )
    return confirmation
