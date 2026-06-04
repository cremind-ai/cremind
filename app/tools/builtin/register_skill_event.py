"""Register Skill Event built-in tool.

Records a (conversation, skill, event_type, action) tuple so that whenever a
new ``*.md`` file appears in ``<skill_dir>/events/<event_type>/`` (produced by
the skill's own listener daemon), the reasoning agent re-runs ``action`` with
the file content appended — and streams the result into the conversation.

The LLM is steered toward this tool by a hint block injected into the loaded
skill's instruction text whenever a skill declares ``metadata.events``. The
tool itself only appears in the action enum once at least one such skill is
loaded in the current run (filtered in :mod:`app.agent.reasoning_agent`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.events.manager import get_event_manager
from app.storage import get_event_subscription_storage
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.utils.skill_source import lookup_skill_source
from app.tools.ids import slugify
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Register Skill Event"


def _resolve_skill(skill_name: str, profile: str) -> Optional[tuple[str, str]]:
    """Resolve a user-supplied skill name to ``(tool_id, source_dir)``.

    Skill rows are keyed by ``<profile>__<slug>`` (see
    :mod:`app.tools.registry`). The hint we inject into the system prompt
    asks the LLM to pass that tool_id form directly, but for resilience we
    also accept a bare slug or the original SKILL.md ``name`` value
    (e.g. ``email-cli``); a leading ``<profile>__`` is stripped before
    re-slugging so a stale prefix on a different profile still resolves.
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

TOOL_CONFIG: ToolConfig = {
    "name": "register_skill_event",
    "display_name": "Register Skill Event",
    "default_model_group": "low",
    # System tool: registered at runtime so the reasoning agent can auto-route
    # skill-event subscriptions through it, but suppressed from the Settings
    # UI / Setup Wizard so users can't disable it (disabling would break the
    # event-subscription flow).
    "hidden": True,
    "llm_parameters": {
        "tool_instructions": (
            "This tool is for subscribing the current conversation to events emitted by a loaded skill."
        ),
        "system_prompt": (
            "You convert a registration request into a structured tool call.\n"
            "Always call register_skill_event with trigger and action.\n"
            "trigger is an array of one or more event names declared by the skill.\n"
            "action is the string command to run when any of those events fire. "
            "The action must only contain executable commands and must not include "
            "any event-related information.\n"
            "E.g. 'When a new email is received, please summarize it for me' -> "
            "trigger: new email, action: 'summarize latest email'."
        ),
    },
}


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


class RegisterSkillEventTool(BuiltInTool):
    name: str = "register_skill_event"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "trigger": {
                "description": "An array of one or more event names declared by the skill",
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True,
            },
            "action": {
                "type": "string",
                "description": "The string command to run when any of those events fire."
            },
        },
        "required": ["trigger", "action"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: Optional[str] = arguments.get("_profile")
        context_id: Optional[str] = arguments.get("_context_id")
        skill_id: str = (arguments.get("_skill_id") or "").strip()
        skill_source: str = (arguments.get("_skill_source") or "").strip()
        triggers: List[str] = _normalize_triggers(arguments.get("trigger"))
        action: str = (arguments.get("action") or "").strip()

        if not profile:
            return _err("Internal error: profile not provided to register_skill_event.")
        if not context_id:
            return _err("Internal error: context_id not provided to register_skill_event.")
        if not skill_id:
            return _err(
                "Internal error: _skill_id was not injected by the dispatcher. "
                "register_skill_event is invoked automatically and must be pinned "
                "to a specific skill."
            )
        if not skill_source:
            # Fall back to looking up the source from storage if the dispatcher
            # only injected the id — covers edge cases without breaking.
            looked_up = lookup_skill_source(skill_id, profile)
            if not looked_up:
                return _err(
                    f"Skill '{skill_id}' was not found for profile '{profile}'. "
                    f"Make sure the skill is installed and enabled."
                )
            skill_source = looked_up
        if not triggers:
            return _err("trigger is required (non-empty array of event names).")
        if not action:
            return _err("action is required.")

        canonical_skill_id = skill_id
        source_dir_str = skill_source
        source_dir = Path(source_dir_str)

        events = _read_events_metadata(source_dir)
        valid_names = [e["name"] for e in events]
        if not valid_names:
            return _err(
                f"Skill '{canonical_skill_id}' does not declare any events in "
                f"its metadata.events. Cannot register a trigger."
            )
        invalid = [t for t in triggers if t not in valid_names]
        if invalid:
            return _err(
                f"trigger(s) {invalid} are not declared by skill "
                f"'{canonical_skill_id}'. Valid triggers: "
                f"{', '.join(valid_names)}."
            )

        # Resolve (or create) the conversation row. On the very first user
        # turn the executor has not yet persisted a conversation row — that
        # only happens after the reasoning loop finishes (see
        # ``app/agent/executor.py``). Creating it eagerly here gives the
        # subscription a valid FK target without waiting for another turn,
        # and the executor's later ``get_or_create_conversation`` call is a
        # no-op because we now share the same ``context_id``.
        from app.storage import get_conversation_storage

        conv_storage = get_conversation_storage()
        try:
            conv = await conv_storage.get_or_create_conversation(
                profile=profile, context_id=context_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_skill_event: get_or_create_conversation failed")
            return _err(f"Could not resolve the active conversation: {exc}")
        if conv is None:
            return _err("Could not resolve the active conversation.")
        conversation_id = conv["id"]

        # Persist + watch using the canonical tool_id so every entry agrees
        # regardless of the surface form the LLM happened to pass. One row +
        # one watcher per (conversation, skill, trigger). Multiple triggers in
        # the same call become independent subscriptions that share an action.
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
                logger.exception("register_skill_event: insert failed")
                return _err(
                    f"Failed to save subscription for trigger '{trigger}': {exc}"
                )
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
                    f"register_skill_event: ensure_watcher failed for '{trigger}'"
                )
                watcher_failures.append(f"'{trigger}': {exc}")

        # Push the new subscriptions to any open events-page SSE subscribers
        # so the admin UI lights them up without a manual refresh. Imported
        # locally to avoid pulling api.events into tool-import time.
        try:
            from app.api.events import publish_skill_events_admin_changed
            publish_skill_events_admin_changed(profile)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"register_skill_event: admin-bus publish failed: {exc}")

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
                f"Subscribed this conversation to {len(triggers)} events of "
                f"skill '{canonical_skill_id}': {trig_list}. Whenever any of "
                f"these events fires (under {source_dir / 'events'}/<event_type>/), "
                f"I'll run: {action}."
            )
        if watcher_failures:
            confirmation += (
                "\n\nNote: subscriptions were saved, but some watchers failed "
                f"to start: {'; '.join(watcher_failures)}. Check server logs."
            )

        logger.info(
            f"register_skill_event: conv={conversation_id} "
            f"skill={canonical_skill_id} triggers={triggers} "
            f"ids={[r['id'] for r in rows]}"
        )
        return BuiltInToolResult(
            content=[{"type": "text", "text": confirmation}]
        )


def _err(message: str) -> BuiltInToolResult:
    return BuiltInToolResult(content=[{"type": "text", "text": message}])


def get_tools(config: dict) -> list[BuiltInTool]:
    return [RegisterSkillEventTool()]


def get_prepare_tools():
    """Return a per-request callback that injects ``trigger``'s dynamic enum.

    The dispatcher pins the target skill via ``_skill_id``/``_skill_source``
    in ``arguments``. This callback reads SKILL.md from that source dir and
    populates the ``trigger`` property's ``enum`` + ``description`` so the
    child LLM can only pick a declared event name.
    """

    def prepare_tools(query, tools, *, arguments=None, **_):
        if not arguments:
            return tools
        source = (arguments.get("_skill_source") or "").strip() if isinstance(arguments, dict) else ""
        if not source:
            return tools
        try:
            events = _read_events_metadata(Path(source))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"register_skill_event prepare_tools: failed to read events "
                f"from {source}: {exc}"
            )
            return tools
        if not events:
            return tools
        enum_values = [e["name"] for e in events if e.get("name")]
        if not enum_values:
            return tools
        desc_lines = [
            f"- {e['name']}: {e.get('description', '')}".rstrip(": ").rstrip()
            for e in events
            if e.get("name")
        ]
        for tool in tools:
            fn = tool.get("function") or {}
            if fn.get("name") != "register_skill_event":
                continue
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            trig = props.get("trigger")
            if isinstance(trig, dict):
                items_schema = trig.get("items")
                if isinstance(items_schema, dict):
                    items_schema["enum"] = enum_values
                else:
                    trig["items"] = {"type": "string", "enum": enum_values}
                trig["description"] = (
                    "Array of one or more event names this conversation should "
                    "subscribe to. Available events:\n" + "\n".join(desc_lines)
                )
            break
        return tools

    return prepare_tools
