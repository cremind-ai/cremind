"""``request_user_input`` built-in tool — park an event run 'pending'.

Event runs fire automatically with no user watching live (see
:mod:`app.events.run_dispatcher`). When such a run genuinely needs the user to
confirm a risky action or supply missing information, the agent calls this tool
with one clear question. The tool records the question in
:mod:`app.events.run_state` (keyed by the stream ``run_id`` read from
``current_task_id_var``); the reasoning loop then ends the turn, and the stream
runner marks the ``event_runs`` row ``pending``. The user's reply, typed into the
run's mini-chat, arrives as the next turn and resumes the run.

Lifecycle is *system-managed*: the tool is ``hidden`` (never shown in Settings)
and the reasoning agent exposes it only on event runs. On a normal chat run it is
withheld — a chat turn asks its question in plain text and simply ends.
"""

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var


SERVER_NAME = "Request User Input"


TOOL_CONFIG: ToolConfig = {
    "name": "request_user_input",
    "display_name": "Request User Input",
    # System-managed: present in the registry but suppressed from the Settings
    # UI. The reasoning agent exposes it only inside event-run conversations.
    "hidden": True,
}


class RequestUserInputTool(BuiltInTool):
    name: str = "request_user_input"
    description: str = (
        "Pause this automated event run and ask the user a single question when "
        "you genuinely need confirmation for a risky/irreversible action or "
        "missing information you cannot obtain yourself. Calling this parks the "
        "run as 'pending' and ends your turn; the user's reply (typed into the "
        "run's chat) arrives as your next turn. Ask ONE clear question. Do not "
        "use this for progress updates, and never ask when you can reasonably "
        "proceed on your own."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The single, clear question or confirmation request to show "
                    "the user."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of suggested quick-reply choices for the user."
                ),
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        question = str(arguments.get("question") or "").strip()
        choices = arguments.get("choices")
        if not question:
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    "No question provided. Provide a 'question' to ask the user."
                )}]
            )

        run_id = current_task_id_var.get()
        if not run_id:
            # No active run context (e.g. the tool leaked onto a non-event run).
            # Degrade gracefully rather than parking nothing.
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    "request_user_input is only available inside an automated "
                    "event run. Ask your question in plain text instead."
                )}]
            )

        prompt = question
        if isinstance(choices, list) and choices:
            opts = ", ".join(str(c) for c in choices if str(c).strip())
            if opts:
                prompt = f"{question}\n\nSuggested options: {opts}"

        from app.events import run_state
        run_state.mark_pending(run_id, prompt)
        logger.info(f"[request_user_input] run={run_id} parked pending: {question[:150]}")

        return BuiltInToolResult(
            content=[{"type": "text", "text": (
                "The question has been shown to the user and the run is now "
                "parked as pending. End your turn now — do not call any more "
                "tools. The user's reply will arrive as your next turn."
            )}]
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [RequestUserInputTool()]
