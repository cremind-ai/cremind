"""``update_todos`` built-in tool — Plan-mode execution progress.

While executing an approved plan the agent maintains a live todo list. Each call
passes the FULL current list (every item with a status); the tool overwrites the
snapshot in :mod:`app.agent.plan_state` and queues a ``todos`` UI event so the
pinned todo panel updates (and highlights the changed item). Unlike
``ask_user_question`` / ``write_plan`` this tool does NOT end the turn — the
agent keeps working between updates.

Lifecycle is *system-managed*: the tool is ``hidden`` and the reasoning agent
exposes it during a Plan-mode planning/execution turn and during an automated
event run (so a multi-step event action can drive the same live panel).
"""

from typing import Any, Dict, List

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var


SERVER_NAME = "Update Todos"


TOOL_CONFIG: ToolConfig = {
    "name": "update_todos",
    "display_name": "Update Todos",
    "hidden": True,
}

_STATUSES = ("pending", "in_progress", "completed")


class UpdateTodosTool(BuiltInTool):
    name: str = "update_todos"
    description: str = (
        "Plan-mode execution or an automated event run: maintain your todo list "
        "while carrying out the approved plan (or a multi-step event action). Pass "
        "the FULL current list every time (not a delta): each item has `content` "
        "and `status` (pending | in_progress | completed). Call it right after "
        "reading the plan (seed the list), when you start an item (mark it "
        "in_progress — keep at most one in_progress at a time), and when you "
        "complete one. This updates the user's live todo panel. It does not end "
        "your turn; keep executing between updates."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": list(_STATUSES)},
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["todos"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        raw = arguments.get("todos")
        if not isinstance(raw, list):
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "Provide 'todos' as a list of {content, status} items."
            )}])

        normalized: List[Dict[str, Any]] = []
        for i, t in enumerate(raw):
            if not isinstance(t, dict):
                continue
            content = str(t.get("content") or "").strip()
            if not content:
                continue
            status = t.get("status")
            if status not in _STATUSES:
                status = "pending"
            normalized.append({
                "id": str(t.get("id") or f"t{i}"),
                "content": content,
                "status": status,
            })

        run_id = current_task_id_var.get()
        if not run_id:
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "update_todos is only available inside a Plan-mode execution run "
                "or an automated event run."
            )}])

        from app.agent import plan_state
        plan_state.set_todos(run_id, normalized)
        plan_state.push_emit(run_id, {"event": "todos", "data": {"todos": normalized}})

        done = sum(1 for t in normalized if t["status"] == "completed")
        logger.info(f"[update_todos] run={run_id} {done}/{len(normalized)} completed")
        return BuiltInToolResult(content=[{"type": "text", "text": (
            f"Todos updated: {done}/{len(normalized)} completed. Keep executing."
        )}])


def get_tools(config: dict) -> list[BuiltInTool]:
    return [UpdateTodosTool()]
