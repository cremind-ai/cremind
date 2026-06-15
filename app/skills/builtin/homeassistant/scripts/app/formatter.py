"""Output formatting: YAML frontmatter, event markdown, plain-text tables."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


_MAX_ATTR_CHARS = 2000


def _yaml_quote(value) -> str:
    if value is None:
        return '""'
    s = str(value)
    if s == "":
        return '""'
    needs_quote = any(c in s for c in ":#&*!|>'\"%@`\\") or s.startswith(("-", "?", "!"))
    needs_quote = needs_quote or "\n" in s or s.strip() != s
    if not needs_quote:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def received_at_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _attributes_json(attributes: dict | None) -> str:
    try:
        s = json.dumps(attributes or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        s = str(attributes)
    if len(s) > _MAX_ATTR_CHARS:
        s = s[:_MAX_ATTR_CHARS] + "...(truncated)"
    return s


def format_event_markdown(entity: dict[str, Any], *, event_type: str) -> str:
    entity_id = entity.get("entity_id", "")
    friendly = entity.get("friendly_name") or entity_id
    state = entity.get("state", "")
    previous = entity.get("previous_state", "")
    lines = [
        "---",
        f"entity_id: {_yaml_quote(entity_id)}",
        f"friendly_name: {_yaml_quote(friendly)}",
        f"domain: {_yaml_quote(entity.get('domain', ''))}",
        f"state: {_yaml_quote(state)}",
        f"previous_state: {_yaml_quote(previous)}",
        f"last_changed: {_yaml_quote(entity.get('last_changed', ''))}",
        f"last_updated: {_yaml_quote(entity.get('last_updated', ''))}",
        f"attributes: {_yaml_quote(_attributes_json(entity.get('attributes')))}",
        f"event_type: {_yaml_quote(event_type)}",
        f"received_at: {_yaml_quote(received_at_iso())}",
        "---",
        "",
        f"{friendly} changed from {previous or 'unknown'} to {state or 'unknown'}.",
        "",
    ]
    return "\n".join(lines)


def format_entities_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no entities)"
    parts = []
    for r in rows:
        label = r.get("friendly_name") or r.get("entity_id")
        parts.append(f"- {label}  [{r.get('entity_id')}] = {r.get('state')}")
    return "\n".join(parts)


def format_state_table(row: dict[str, Any]) -> str:
    if not row:
        return "(no state)"
    lines = [
        f"{row.get('entity_id')} = {row.get('state')}",
        f"  last_changed: {row.get('last_changed')}",
    ]
    attrs = row.get("attributes") or {}
    if attrs:
        lines.append("  attributes:")
        for k, v in attrs.items():
            lines.append(f"    {k}: {v}")
    return "\n".join(lines)
