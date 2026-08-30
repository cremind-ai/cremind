"""``get_event_task_results`` built-in tool — pull waiting event-task results.

An *event task* is a one-shot event the agent registered mid-flow because it
cannot finish without the outcome ("open the PR, wait for CI, then merge"). When
one finishes while this conversation is idle, its result arrives on its own as a
new turn and this tool is never needed.

When it finishes while a turn is RUNNING, injecting it would only queue it
behind that turn (one conversation runs one turn at a time), so it waits in the
conversation's inbox and a short notice interrupts the running turn at its next
step. This tool is how the agent acts on that notice: it hands over the full
text of every waiting result and marks them delivered. Ignoring the notice is
equally valid — whatever is unread is injected as one turn the moment the turn
ends.

Lifecycle is *system-managed*: the tool is ``hidden`` (never shown in Settings,
never in the built-in catalogue — so the ``description`` below is the model's
only cue) and the reasoning agent withholds it inside event runs, which have no
inbox of their own and cannot register tasks.
"""

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var


SERVER_NAME = "Get Event Task Results"


TOOL_CONFIG: ToolConfig = {
    "name": "get_event_task_results",
    "display_name": "Get Event Task Results",
    # System-managed: present in the registry but suppressed from the Settings
    # UI. The reasoning agent withholds it inside event-run conversations.
    "hidden": True,
}

_NO_CONTEXT = (
    "get_event_task_results is only available inside a live conversation turn. "
    "There is nothing waiting to read here."
)


class GetEventTaskResultsTool(BuiltInTool):
    name: str = "get_event_task_results"
    description: str = (
        "Read the results of one-shot event tasks that finished while this "
        "conversation was busy and are waiting to be handed to you. Call this "
        "when a notice tells you results are waiting AND the outcome affects "
        "what you should do next — for example you registered a task earlier in "
        "this flow and have now reached the step that was waiting on it. Takes "
        "no arguments: one call returns EVERY result currently waiting and hands "
        "them all over, so call it once, not once per result. If you do NOT call "
        "it, the waiting results are delivered automatically as a new turn the "
        "moment this turn ends — so ignoring the notice is a legitimate choice "
        "when you are in the middle of something unrelated. Do not re-register a "
        "task that has just finished."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        from app.events import task_result_inbox

        run_id = current_task_id_var.get()
        # The conversation is resolved through the run binding rather than from
        # ``_context_id``: a channel-backed conversation carries the platform's
        # own chat id as its context_id, and a conversation id can be renamed, so
        # neither is a reliable key. No binding (e.g. the legacy executor path)
        # simply means there is no inbox to read.
        conversation_id = task_result_inbox.conversation_for_run(run_id or "")
        if not conversation_id:
            return BuiltInToolResult(content=[{"type": "text", "text": _NO_CONTEXT}])

        from app.events.event_task_delivery import read_origin_inbox

        text, depths = await read_origin_inbox(
            conversation_id=conversation_id,
            profile=str(arguments.get("_profile") or ""),
        )
        # Everything waiting has now been handed over, so any notice still
        # parked describes a row this call just delivered. Left in place it
        # would interrupt the user on the next step about a result the agent is
        # already holding. Discarded only on success: a read that raised (Stop,
        # a DB error) released its claims, and those rows are still undelivered.
        task_result_inbox.drain_notices(run_id or "")
        if depths and run_id:
            # A turn that pulls a result never mints a new trigger_event, so
            # without this the wait→read→register chain would restart the
            # runaway counter at zero on every hop.
            task_result_inbox.note_consumed_depth(run_id, max(depths))
        logger.info(
            f"[get_event_task_results] run={run_id} handed over {len(depths)} result(s)"
        )
        return BuiltInToolResult(content=[{"type": "text", "text": text}])


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [GetEventTaskResultsTool()]
