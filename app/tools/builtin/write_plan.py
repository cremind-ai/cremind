"""``write_plan`` built-in tool — Plan-mode plan authoring.

After clarifying questions are answered, the Plan-mode agent calls this tool with
a detailed Markdown plan. The tool writes the plan to a durable per-conversation
file (``<CREMIND_SYSTEM_DIR>/<profile>/plans/<conversation_id>/<name>.md`` — see
:mod:`app.utils.plans_dir`), records it in :mod:`app.agent.plan_state` and queues
a ``plan_ready`` UI event carrying the markdown + path. The reasoning loop then
ends the turn; the plan is shown to the user with Accept/Cancel, and the user's
decision arrives as the next turn.

Lifecycle is *system-managed*: the tool is ``hidden`` and the reasoning agent
exposes it only during a Plan-mode planning turn.
"""

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var


SERVER_NAME = "Write Plan"


TOOL_CONFIG: ToolConfig = {
    "name": "write_plan",
    "display_name": "Write Plan",
    "hidden": True,
}


class WritePlanTool(BuiltInTool):
    name: str = "write_plan"
    description: str = (
        "Plan mode only: save your detailed implementation plan as a Markdown "
        "file and show it to the user for approval. Provide `filename` (a short "
        "name like 'refactor-auth.md'), an optional `title`, and `markdown` (the "
        "full plan). Calling this ends your turn — do not call any other tool "
        "afterwards. The user reviews the plan and Accepts or Cancels; on Accept "
        "you resume in the execution phase and carry it out."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Plan file name, e.g. 'refactor-auth.md'. Basename only.",
            },
            "title": {
                "type": "string",
                "description": "Human-readable plan title.",
            },
            "markdown": {
                "type": "string",
                "description": "The full plan as Markdown.",
            },
        },
        "required": ["filename", "markdown"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        markdown = str(arguments.get("markdown") or "").strip()
        filename = str(arguments.get("filename") or "plan.md").strip()
        title = str(arguments.get("title") or "").strip()
        if not markdown:
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "No plan content provided. Provide the plan in 'markdown'."
            )}])

        run_id = current_task_id_var.get()
        if not run_id:
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "write_plan is only available inside a Plan-mode run. Present your "
                "plan in plain text instead."
            )}])

        context_id = arguments.get("_context_id")
        profile = arguments.get("_profile") or arguments.get("profile") or "default"

        # Resolve the calling conversation (context_id == conversation.context_id;
        # fall back to a direct id lookup, mirroring compact_conversation).
        conversation_id = None
        try:
            from app.events.runner import get_conversation_storage
            cs = get_conversation_storage()
            conv = None
            if context_id:
                conv = await cs.get_conversation_by_context(profile=profile, context_id=context_id)
                if conv is None:
                    conv = await cs.get_conversation(context_id)
            if conv is not None:
                conversation_id = conv["id"]
        except Exception:  # noqa: BLE001
            logger.exception("[write_plan] failed to resolve conversation")

        if not conversation_id:
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "Could not resolve this conversation to save the plan. Present your "
                "plan in plain text instead."
            )}])

        try:
            from app.utils.plans_dir import plan_file_path
            path = plan_file_path(profile, conversation_id, filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(markdown)
        except Exception as e:  # noqa: BLE001
            logger.exception("[write_plan] failed to write plan file")
            return BuiltInToolResult(structured_content={
                "error": "WritePlanFailed", "message": str(e),
            })

        import os
        saved_name = os.path.basename(path)
        payload = {
            "path": path,
            "filename": saved_name,
            "title": title,
            "markdown": markdown,
            "status": "awaiting_approval",
        }
        from app.agent import plan_state
        plan_state.mark_plan(run_id, payload)
        plan_state.push_emit(run_id, {"event": "plan_ready", "data": payload})
        logger.info(f"[write_plan] run={run_id} saved plan to {path}")

        return BuiltInToolResult(content=[{"type": "text", "text": (
            f"Plan saved to {path} and shown to the user for approval. End your "
            "turn now — do not call any more tools. Wait for the user to Accept "
            "(then you execute it) or Cancel."
        )}])


def get_tools(config: dict) -> list[BuiltInTool]:
    return [WritePlanTool()]
