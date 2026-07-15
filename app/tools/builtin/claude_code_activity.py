"""Translate Claude Agent SDK messages into generic agent-activity steps.

Keeps the Claude-specific mapping (SDK message/block shapes → user-facing step
labels) out of the agent-agnostic :mod:`app.agent.agent_activity` module, so a
future Codex translator can sit beside this one and feed the same panel.

The SDK is imported lazily by the runner and may be absent at import time
(the ``claude_code`` feature is opt-in). This module therefore dispatches by
duck-typing (class name + attributes) and never imports ``claude_agent_sdk`` —
so importing it during built-in tool registration is always safe.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agent.agent_activity import AgentActivity

_MODEL_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _context_tokens_from(usage: Any) -> int:
    """Prompt-token size of a Claude request from its ``usage`` dict.

    Sums the three prompt components (fresh input + cache read + cache write);
    output is excluded so this mirrors "context currently occupied". Returns 0
    for a missing/non-dict usage.
    """
    if not isinstance(usage, dict):
        return 0
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def _context_window_for_model(model: Any) -> Optional[int]:
    """Resolve a model's context window from the pricing catalog, or ``None``.

    Tries the exact model id, then retries with a trailing ``-YYYYMMDD`` date
    suffix stripped (Claude Code reports dated ids like
    ``claude-sonnet-4-6-20260203`` while the catalog keys the alias id).
    Returns ``None`` (never the default window) when unresolved, so the UI
    degrades to a tokens-only indicator with no percentage.
    """
    if not model:
        return None
    try:
        from app.lib.llm.pricing import context_window_for

        model = str(model)
        window = context_window_for("anthropic", model)
        if window is None:
            window = context_window_for("anthropic", _MODEL_DATE_SUFFIX_RE.sub("", model))
        return window
    except Exception:  # noqa: BLE001 — window lookup is best-effort
        return None


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


def tool_use_label(name: str, tool_input: dict) -> tuple[str, Optional[str]]:
    """Return ``(label, detail)`` for a Claude Code ToolUseBlock.

    Public so the runner/tests can reuse the exact mapping.
    """
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        cmd = _short(inp.get("command"), 120)
        detail_parts = [str(inp.get("command", ""))]
        if inp.get("description"):
            detail_parts.append(str(inp["description"]))
        return f"$ {cmd}", "\n".join(p for p in detail_parts if p)
    if name == "Read":
        loc = inp.get("file_path", "")
        rng = ""
        if inp.get("offset") or inp.get("limit"):
            rng = f" (offset={inp.get('offset')}, limit={inp.get('limit')})"
        return f"Reading {_short(loc, 100)}", f"{loc}{rng}" if loc else None
    if name in ("Edit", "MultiEdit"):
        loc = inp.get("file_path", "")
        return f"Editing {_short(loc, 100)}", _short(inp.get("old_string"), 300)
    if name == "Write":
        loc = inp.get("file_path", "")
        content = inp.get("content") or ""
        return f"Writing {_short(loc, 100)}", f"{len(str(content))} chars"
    if name == "NotebookEdit":
        loc = inp.get("notebook_path", "")
        return f"Editing notebook {_short(loc, 100)}", None
    if name in ("Glob", "Grep"):
        pattern = inp.get("pattern", "")
        extra = inp.get("path") or inp.get("glob") or ""
        return f"Searching {_short(pattern, 90)}", str(extra) or None
    if name == "WebFetch":
        return f"Fetching {_short(inp.get('url'), 100)}", _short(inp.get("prompt"), 300)
    if name == "WebSearch":
        return f"Searching web: {_short(inp.get('query'), 100)}", None
    if name == "Task":
        desc = inp.get("description") or inp.get("subagent_type") or "task"
        return f"Sub-agent: {_short(desc, 90)}", _short(inp.get("prompt"), 300)
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        n = len(todos) if isinstance(todos, list) else 0
        lines = []
        if isinstance(todos, list):
            for t in todos[:12]:
                if isinstance(t, dict):
                    lines.append(f"[{t.get('status', '?')}] {t.get('content', '')}")
        return f"Updating todos ({n} items)", _short("\n".join(lines), 400)
    # Fallback for MCP tools and anything unmapped.
    try:
        detail = json.dumps(inp, ensure_ascii=False)
    except (TypeError, ValueError):
        detail = str(inp)
    return f"Tool: {name}", _short(detail, 300)


def _result_preview(content: Any) -> str:
    if isinstance(content, str):
        return _short(content, 300)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif block.get("text"):
                    parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return _short("\n".join(parts), 300)
    return _short(content, 300)


async def apply_sdk_message(activity: AgentActivity, message: Any) -> None:
    """Map one SDK message to activity steps (best-effort; never raises).

    Handles ``AssistantMessage`` (thinking / text / tool_use, plus its live
    context-usage) and tool-result blocks carried on ``UserMessage``.
    ``SystemMessage`` and the terminal handling of ``ResultMessage`` are owned
    by the runner (session id / finish); ``ResultMessage.usage`` is folded here
    only as a context fallback when no per-message usage was seen.
    """
    try:
        cls = type(message).__name__
        content = getattr(message, "content", None)
        if cls == "AssistantMessage":
            # Sub-agent (Task tool) messages run in their own context — only the
            # main loop's usage reflects the session's context occupancy.
            if not getattr(message, "parent_tool_use_id", None):
                ctx = _context_tokens_from(getattr(message, "usage", None))
                if ctx > 0:
                    await activity.update_usage(
                        {
                            "context_tokens": ctx,
                            "context_window": _context_window_for_model(
                                getattr(message, "model", None)
                            ),
                        }
                    )
            if isinstance(content, list):
                for block in content:
                    await _apply_block(activity, block)
        elif cls == "UserMessage" and isinstance(content, list):
            for block in content:
                # Only tool results ride on UserMessage in the agent loop.
                if type(block).__name__ == "ToolResultBlock":
                    await _apply_block(activity, block)
        elif cls == "ResultMessage" and getattr(activity, "usage", None) is None:
            # Fallback for older CLIs that omit per-message usage. ResultMessage.usage
            # is cumulative for the whole run, so it may overstate live context —
            # used only when nothing better was seen.
            ctx = _context_tokens_from(getattr(message, "usage", None))
            if ctx > 0:
                window = None
                model_usage = getattr(message, "model_usage", None)
                if isinstance(model_usage, dict) and len(model_usage) == 1:
                    window = _context_window_for_model(next(iter(model_usage)))
                await activity.update_usage(
                    {"context_tokens": ctx, "context_window": window}
                )
    except Exception:  # noqa: BLE001 — activity translation is best-effort
        return


async def _apply_block(activity: AgentActivity, block: Any) -> None:
    kind = type(block).__name__
    if kind == "ThinkingBlock":
        thinking = getattr(block, "thinking", "") or ""
        if thinking.strip():
            await activity.add_step(
                kind="thinking",
                label=_first_line(thinking),
                detail=thinking,
            )
    elif kind == "TextBlock":
        text = getattr(block, "text", "") or ""
        if text.strip():
            await activity.add_step(
                kind="text",
                label=_first_line(text),
                detail=text,
            )
    elif kind == "ToolUseBlock":
        name = getattr(block, "name", "") or "tool"
        tool_input = getattr(block, "input", {}) or {}
        label, detail = tool_use_label(name, tool_input)
        await activity.add_step(
            kind="tool_use",
            label=label,
            detail=detail,
            step_id=getattr(block, "id", None),
            status="running",
        )
    elif kind == "ToolResultBlock":
        tool_use_id = getattr(block, "tool_use_id", None)
        if not tool_use_id:
            return
        is_error = bool(getattr(block, "is_error", False))
        preview = _result_preview(getattr(block, "content", None))
        await activity.resolve_step(
            tool_use_id,
            status="error" if is_error else "ok",
            detail_suffix=(f"→ {preview}" if preview else None),
        )
