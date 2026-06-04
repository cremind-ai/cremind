"""Sleep built-in tool.

Pauses execution for a specified number of seconds.
"""

import asyncio
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig


SERVER_NAME = "Sleep"

MAX_SLEEP = 300  # 5 minutes — fallback when no per-profile override is set


class Var:
    MAX_SECONDS = "MAX_SECONDS"


TOOL_CONFIG: ToolConfig = {
    "name": "sleep",
    "display_name": "Sleep",
    "default_model_group": "low",
    "hidden": True,
    "llm_parameters": {
        "tool_instructions": "Pause execution for a specified number of seconds.",
        "system_prompt": ("Don't answer any questions or provide any information.\n"
        "Always use 'sleep' tool call for any user input"
        "E.g. 'sleep 2 seconds {\"seconds\": 2}'"),
    },
    "required_config": {
        Var.MAX_SECONDS: {
            "description": (
                "Maximum number of seconds the sleep tool will accept in a "
                "single call. Default: 300 (5 minutes)."
            ),
            "type": "number",
            "default": MAX_SLEEP,
        },
    },
}


class SleepTool(BuiltInTool):
    name: str = "sleep"
    description: str = "Pause execution for a specified number of seconds."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "Number of seconds to sleep.",
            }
        },
        "required": ["seconds"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        seconds = arguments.get("seconds", 0)
        variables = arguments.get("_variables") or {}
        try:
            max_sleep = float(variables.get(Var.MAX_SECONDS) or MAX_SLEEP)
        except (TypeError, ValueError):
            max_sleep = MAX_SLEEP

        if not isinstance(seconds, (int, float)) or seconds < 0:
            return BuiltInToolResult(
                content=[{"type": "text", "text": "seconds must be a non-negative number"}]
            )

        if seconds > max_sleep:
            return BuiltInToolResult(
                content=[{"type": "text", "text": f"seconds exceeds maximum of {max_sleep}"}]
            )

        await asyncio.sleep(seconds)

        if seconds == int(seconds):
            label = f"{int(seconds)}s"
        else:
            label = f"{seconds:.1f}s"

        return BuiltInToolResult(
            content=[{"type": "text", "text": f"slept {label}"}]
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [SleepTool()]
