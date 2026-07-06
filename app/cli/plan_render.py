"""Plain-text formatters for Plan-mode SSE events.

Shared by the raw stream renderer (`app/cli/streaming.py`) and the TUI renderer
(`app/cli/tui/renderer.py`) so the two never drift. Each function takes the
event's inner payload (``event.data["data"]``) and returns a list of plain,
ANSI-free lines. All parse defensively — missing/wrong-typed keys are skipped.

Payload shapes (mirror what the server publishes):
    ask_user_question: {"questions": [{"id","question","description",
                        "options":[{"label","description"}],
                        "allow_free_text","multi_select"}]}
    plan_ready:        {"path","filename","title","markdown"}
    todos:             {"todos": [{"id","content","status"}]}
"""

from __future__ import annotations

from typing import Any


_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def questions_lines(data: dict[str, Any]) -> list[str]:
    if not isinstance(data, dict):
        return []
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        return []
    lines = ["? The agent needs your input:"]
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            continue
        lines.append(f"{i}. {q.get('question', '')}")
        desc = q.get("description")
        if isinstance(desc, str) and desc.strip():
            lines.append(f"   {desc.strip()}")
        options = q.get("options")
        if isinstance(options, list):
            for j, opt in enumerate(options):
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or "").strip()
                if not label:
                    continue
                letter = _LETTERS[j] if j < len(_LETTERS) else str(j + 1)
                odesc = str(opt.get("description") or "").strip()
                lines.append(f"   {letter}) {label}" + (f" — {odesc}" if odesc else ""))
        if q.get("allow_free_text") is not False:
            lines.append("   (you may also answer in free text)")
    return lines


def plan_hint_lines(data: dict[str, Any], conversation_id: str = "") -> list[str]:
    if not isinstance(data, dict):
        return []
    filename = str(data.get("filename") or "plan.md")
    title = str(data.get("title") or "").strip()
    path = str(data.get("path") or "").strip()
    header = f"Plan ready: {filename}" + (f" — {title}" if title else "")
    lines = [header]
    if path:
        lines.append(f"saved to: {path}")
    accept_cmd = 'cremind conv send <id> "accept" --mode plan'
    if conversation_id:
        accept_cmd = f'cremind conv send {conversation_id} "accept" --mode plan'
    lines.append("Reply 'accept' to execute, or describe changes; 'cancel' to discard:")
    lines.append(f"  {accept_cmd}")
    return lines


def todos_lines(data: dict[str, Any]) -> list[str]:
    if not isinstance(data, dict):
        return []
    todos = data.get("todos")
    if not isinstance(todos, list) or not todos:
        return []
    marks = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    lines = ["Todos:"]
    for t in todos:
        if not isinstance(t, dict):
            continue
        mark = marks.get(str(t.get("status")), "[ ]")
        lines.append(f"{mark} {t.get('content', '')}")
    return lines
