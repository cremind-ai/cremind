"""How a fired file-watcher event is worded for the agent that will handle it.

The payload comes from the watchdog event itself, not from a ``.md`` file on
disk (that is the skill-event shape). Execution lives in
:mod:`app.events.run_dispatcher`, which runs the trigger in its own hidden
conversation and hands the result back to whichever conversation registered the
watcher.
"""

from __future__ import annotations

from typing import Any, Dict


def _format_content(payload: Dict[str, Any]) -> str:
    """Build the content block describing the watchdog event (event_type,
    target_kind, path, watch_name, extension, detected_at, plus src/dest on
    ``moved`` events)."""
    lines = [
        f"event_type: {payload.get('event_type', '')}",
        f"target_kind: {payload.get('target_kind', '')}",
    ]
    src_path = payload.get("src_path")
    dest_path = payload.get("dest_path")
    if payload.get("event_type") == "moved" and src_path and dest_path:
        lines.append(f"src_path: {src_path}")
        lines.append(f"dest_path: {dest_path}")
    else:
        lines.append(f"path: {payload.get('path', '')}")
    lines.extend([
        f"watch_name: {payload.get('watch_name', '')}",
        f"extension: {payload.get('extension', '')}",
        f"detected_at: {payload.get('detected_at', '')}",
    ])
    return "\n".join(lines)


def build_trigger_messages(action: str, payload: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(reasoning_query, bubble_content)`` for a file-watcher event.

    The reasoning ``Input:`` is the action command followed by the event
    details (so the agent can act on what changed). The UI bubble keeps the
    fenced detail block (rendered by stream_runner), so it is unchanged.
    """
    block = _format_content(payload)
    query = f"{action.strip()}\n\n{block}".strip()
    return query, f"---\n{block}\n---"

