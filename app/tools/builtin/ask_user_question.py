"""``ask_user_question`` built-in tool — Plan-mode clarifying questions.

During Plan mode's planning phase the agent must gather missing information from
the user before writing a plan. It calls this tool with 1-4 structured questions
(each with option labels + descriptions and optional free-text), which are
recorded in :mod:`app.agent.plan_state` (keyed by the stream ``run_id``) and
queued as an ``ask_user_question`` UI event. The reasoning loop then ends the
turn — the questions become the parked state and the user's answers, typed into
the composer's question form, arrive as the next turn and resume planning.

Lifecycle is *system-managed*: the tool is ``hidden`` (never shown in Settings)
and the reasoning agent exposes it only during a Plan-mode planning turn. It is a
plan-mode analog of :mod:`app.tools.builtin.request_user_input` (which is
event-run-only) with a richer, multi-question schema for the UI form.
"""

from typing import Any, Dict, List

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var


SERVER_NAME = "Ask User Question"


TOOL_CONFIG: ToolConfig = {
    "name": "ask_user_question",
    "display_name": "Ask User Question",
    # System-managed: present in the registry but suppressed from the Settings
    # UI. The reasoning agent exposes it only on Plan-mode planning turns.
    "hidden": True,
}


class AskUserQuestionTool(BuiltInTool):
    name: str = "ask_user_question"
    description: str = (
        "Plan mode only: ask the user 1-4 clarifying questions before writing a "
        "plan. Each question has a short label, an optional longer description, "
        "and a list of selectable options (each with a label + short description); "
        "the user may also answer in free text. Calling this shows the questions "
        "in a form and ends your turn — do not call any other tool afterwards. The "
        "user's answers arrive as your next turn. Ask only what you genuinely need "
        "to produce a good plan."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question text shown to the user.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional extra context for the question.",
                        },
                        "options": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                            "description": "Selectable answer options.",
                        },
                        "allow_free_text": {
                            "type": "boolean",
                            "description": "Allow an 'Other' free-text answer (default true).",
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "Allow selecting more than one option (default false).",
                        },
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        raw = arguments.get("questions")
        if not isinstance(raw, list) or not raw:
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "No questions provided. Provide 1-4 questions to ask the user."
            )}])

        normalized: List[Dict[str, Any]] = []
        for i, q in enumerate(raw[:4]):
            if not isinstance(q, dict):
                continue
            text = str(q.get("question") or "").strip()
            if not text:
                continue
            options: List[Dict[str, Any]] = []
            for opt in (q.get("options") or []):
                if isinstance(opt, dict) and str(opt.get("label") or "").strip():
                    options.append({
                        "label": str(opt["label"]).strip(),
                        "description": str(opt.get("description") or "").strip(),
                    })
            normalized.append({
                "id": f"q{i}",
                "question": text,
                "description": str(q.get("description") or "").strip(),
                "options": options,
                "allow_free_text": q.get("allow_free_text") is not False,
                "multi_select": bool(q.get("multi_select")),
            })

        if not normalized:
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "No valid questions provided. Each question needs a 'question' string."
            )}])

        run_id = current_task_id_var.get()
        if not run_id:
            # No active run context (tool leaked onto a non-run path). Degrade
            # gracefully rather than parking nothing.
            return BuiltInToolResult(content=[{"type": "text", "text": (
                "ask_user_question is only available inside a Plan-mode run. Ask "
                "your questions in plain text instead."
            )}])

        payload = {"questions": normalized}
        from app.agent import plan_state
        plan_state.mark_questions(run_id, payload)
        plan_state.push_emit(run_id, {"event": "ask_user_question", "data": payload})
        logger.info(f"[ask_user_question] run={run_id} parked {len(normalized)} question(s)")

        return BuiltInToolResult(content=[{"type": "text", "text": (
            "Your questions have been shown to the user in a form. End your turn "
            "now — do not call any more tools. The user's answers will arrive as "
            "your next turn, and you can then write the plan."
        )}])


def get_tools(config: dict) -> list[BuiltInTool]:
    return [AskUserQuestionTool()]
