"""Translate OpenAI Codex SDK notifications into generic agent-activity steps.

The Codex mirror of :mod:`app.tools.builtin.claude_code_activity`: it keeps the
Codex-specific mapping (SDK notification / thread-item shapes → user-facing step
labels) out of the agent-agnostic :mod:`app.agent.agent_activity` module, feeding
the same "Agent Activity" panel Claude Code uses.

The SDK is imported lazily by the runner and may be absent at import time (the
``codex`` feature is opt-in). This module therefore dispatches purely by
duck-typing (notification ``method`` strings + attribute access) and never
imports ``openai_codex`` — so importing it during built-in tool registration is
always safe.

Codex streams a ``Notification(method: str, payload)`` for every event. We map:

* ``item/started`` for a tool-ish item (command execution, file change, MCP /
  dynamic tool call, web search) → a ``tool_use`` step in ``running`` state,
  keyed by the item id;
* ``item/completed`` for that same item → resolve the step ``ok``/``error``;
* ``item/completed`` for an ``agentMessage`` → a ``text`` step, for
  ``reasoning`` → a ``thinking`` step, for ``plan`` → a ``text`` step.

Agent-message deltas, reasoning deltas and token-usage notifications are ignored
(they only bloat the feed; the runner owns token usage). ``turn/completed`` is
ignored here — terminal handling is the runner's job, exactly as
``ResultMessage`` is for Claude Code.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.agent.agent_activity import AgentActivity

# Codex thread-item ``type`` discriminators (see openai_codex.generated.v2_all).
_TOOL_ITEM_TYPES = frozenset(
    {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "webSearch"}
)


def _short(value: Any, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line
    return str(text).strip()


def _unwrap(item: Any) -> Any:
    """Codex ``ThreadItem`` is a pydantic RootModel; the concrete variant is on
    ``.root``. Fall back to the object itself for already-unwrapped/duck items."""
    return getattr(item, "root", item)


def _status_value(status: Any) -> str:
    """Normalise a pydantic enum (or plain string) status to its wire value."""
    return str(getattr(status, "value", status) or "").strip()


def _item_type(item: Any) -> str:
    return str(getattr(item, "type", "") or "")


def _is_tool_item(item_type: str) -> bool:
    return item_type in _TOOL_ITEM_TYPES


def _reasoning_text(item: Any) -> str:
    """Join a reasoning item's summary (preferred) or raw content parts."""
    for attr in ("summary", "content"):
        parts = getattr(item, attr, None)
        if isinstance(parts, (list, tuple)):
            joined = "\n".join(str(p) for p in parts if p)
            if joined.strip():
                return joined
    return ""


def _file_change_label(item: Any) -> tuple[str, Optional[str]]:
    changes = getattr(item, "changes", None) or []
    paths = [getattr(c, "path", "") for c in changes if getattr(c, "path", "")]
    if len(paths) == 1:
        return f"Editing {_short(paths[0], 100)}", None
    if paths:
        return f"Editing {len(paths)} files", _short("\n".join(paths), 300)
    return "Editing files", None


def item_label(item: Any) -> tuple[str, Optional[str]]:
    """Return ``(label, detail)`` for a Codex thread item (tool-ish types).

    Public so the runner/tests can reuse the exact mapping.
    """
    item_type = _item_type(item)
    if item_type == "commandExecution":
        cmd = getattr(item, "command", "") or ""
        return f"$ {_short(cmd, 120)}", str(cmd) or None
    if item_type == "fileChange":
        return _file_change_label(item)
    if item_type == "webSearch":
        return f"Searching web: {_short(getattr(item, 'query', ''), 100)}", None
    if item_type == "mcpToolCall":
        server = getattr(item, "server", "") or ""
        tool = getattr(item, "tool", "") or "tool"
        label = f"Tool: {server}.{tool}" if server else f"Tool: {tool}"
        return label, _json_or_str(getattr(item, "arguments", None))
    if item_type == "dynamicToolCall":
        namespace = getattr(item, "namespace", "") or ""
        tool = getattr(item, "tool", "") or "tool"
        label = f"Tool: {namespace}.{tool}" if namespace else f"Tool: {tool}"
        return label, _json_or_str(getattr(item, "arguments", None))
    return f"Tool: {item_type or 'tool'}", None


def _json_or_str(value: Any) -> Optional[str]:
    if value is None or value == "" or value == {}:
        return None
    try:
        return _short(json.dumps(value, ensure_ascii=False), 300)
    except (TypeError, ValueError):
        return _short(value, 300)


def _tool_item_is_error(item: Any, item_type: str) -> bool:
    status = _status_value(getattr(item, "status", None))
    if status in ("failed", "declined"):
        return True
    if item_type == "commandExecution":
        exit_code = getattr(item, "exit_code", None)
        return exit_code not in (0, None)
    if item_type == "mcpToolCall":
        return getattr(item, "error", None) is not None
    if item_type == "dynamicToolCall":
        return getattr(item, "success", None) is False
    return False


def _tool_item_preview(item: Any, item_type: str) -> str:
    if item_type == "commandExecution":
        return _short(getattr(item, "aggregated_output", None), 300)
    if item_type == "mcpToolCall":
        err = getattr(item, "error", None)
        if err is not None:
            return _short(getattr(err, "message", None) or err, 300)
        return _short(getattr(item, "result", None), 300)
    if item_type == "fileChange":
        changes = getattr(item, "changes", None) or []
        return _short(", ".join(getattr(c, "path", "") for c in changes), 300)
    return ""


async def apply_notification(activity: AgentActivity, notification: Any) -> None:
    """Map one Codex SDK notification to activity steps (best-effort; never raises).

    Handles ``item/started`` (open a running tool step), ``item/completed``
    (resolve a tool step, or append a text/thinking step for messages and
    reasoning), and ``thread/tokenUsage/updated`` (update the panel's live
    context-usage indicator). Deltas are noise and ``turn/completed`` is owned
    by the runner (which keeps the cost-accounting usage on the task).
    """
    try:
        method = str(getattr(notification, "method", "") or "")
        payload = getattr(notification, "payload", None)
        if method == "item/started":
            item = _unwrap(getattr(payload, "item", None))
            item_type = _item_type(item)
            if _is_tool_item(item_type):
                label, detail = item_label(item)
                await activity.add_step(
                    kind="tool_use",
                    label=label,
                    detail=detail,
                    step_id=getattr(item, "id", None),
                    status="running",
                )
        elif method == "item/completed":
            await _apply_completed(activity, _unwrap(getattr(payload, "item", None)))
        elif method == "thread/tokenUsage/updated":
            usage = getattr(payload, "token_usage", None)
            last = getattr(usage, "last", None)
            # Codex input_tokens INCLUDES cached tokens (OpenAI convention) — that
            # total is exactly the last request's prompt size, i.e. context used.
            # Do NOT subtract cached here (unlike the runner's cost-accounting map).
            ctx = int(getattr(last, "input_tokens", 0) or 0)
            if ctx > 0:
                window = getattr(usage, "model_context_window", None)
                await activity.update_usage(
                    {
                        "context_tokens": ctx,
                        "context_window": int(window) if window else None,
                    }
                )
    except Exception:  # noqa: BLE001 — activity translation is best-effort
        return


async def _apply_completed(activity: AgentActivity, item: Any) -> None:
    item_type = _item_type(item)
    if item_type == "agentMessage":
        text = getattr(item, "text", "") or ""
        if text.strip():
            await activity.add_step(kind="text", label=_first_line(text), detail=text)
        return
    if item_type == "reasoning":
        text = _reasoning_text(item)
        if text.strip():
            await activity.add_step(kind="thinking", label=_first_line(text), detail=text)
        return
    if item_type == "plan":
        text = getattr(item, "text", "") or ""
        if text.strip():
            await activity.add_step(kind="text", label="Plan updated", detail=text)
        return
    if _is_tool_item(item_type):
        step_id = getattr(item, "id", None)
        if not step_id:
            return
        is_error = _tool_item_is_error(item, item_type)
        preview = _tool_item_preview(item, item_type)
        await activity.resolve_step(
            step_id,
            status="error" if is_error else "ok",
            detail_suffix=(f"→ {preview}" if preview else None),
        )
