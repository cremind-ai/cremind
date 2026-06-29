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

from app.events.manager import get_event_manager
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
) -> str:
    """Subscribe a conversation to one or more of a skill's declared events.

    The reasoning agent calls this directly when the model invokes a skill tool
    with a ``subscribe`` payload. ``skill_id``/``skill_source`` are pinned by the
    caller to the exact skill whose tool was invoked, so there is no active-skill
    ambiguity. Returns a human-readable confirmation (or error) string that the
    agent appends as the tool result.
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
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_skill_events: insert failed")
            return f"Failed to save subscription for trigger '{trigger}': {exc}"
        rows.append(row)

    watcher_failures: List[str] = []
    for trigger in triggers:
        try:
            get_event_manager().ensure_watcher(
                profile=profile,
                skill_name=canonical_skill_id,
                source_dir=source_dir_str,
                event_type=trigger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"register_skill_events: ensure_watcher failed for '{trigger}'"
            )
            watcher_failures.append(f"'{trigger}': {exc}")

    # Push the new subscriptions to any open events-page SSE subscribers so the
    # admin UI lights them up without a manual refresh. Imported locally to avoid
    # pulling api.events into tool-import time.
    try:
        from app.api.events import publish_skill_events_admin_changed
        publish_skill_events_admin_changed(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"register_skill_events: admin-bus publish failed: {exc}")

    if len(triggers) == 1:
        t = triggers[0]
        confirmation = (
            f"Subscribed this conversation to the '{t}' event of skill "
            f"'{canonical_skill_id}'. Whenever a new event arrives in "
            f"{source_dir / 'events' / t}, I'll run: {action}."
        )
    else:
        trig_list = ", ".join(f"'{t}'" for t in triggers)
        confirmation = (
            f"Subscribed this conversation to {len(triggers)} events of skill "
            f"'{canonical_skill_id}': {trig_list}. Whenever any of these events "
            f"fires (under {source_dir / 'events'}/<event_type>/), I'll run: {action}."
        )
    if watcher_failures:
        confirmation += (
            "\n\nNote: subscriptions were saved, but some watchers failed to "
            f"start: {'; '.join(watcher_failures)}. Check server logs."
        )

    logger.info(
        f"register_skill_events: conv={conversation_id} "
        f"skill={canonical_skill_id} triggers={triggers} "
        f"ids={[r['id'] for r in rows]}"
    )
    return confirmation
