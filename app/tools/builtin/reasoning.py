"""Reasoning ("think") built-in tool.

Gives models *without* native step-by-step reasoning a place to think before
they act. The model calls this tool and passes its chain-of-thought in the
``reasoning`` argument; the tool simply echoes that text back as its result so
the reasoning re-enters the model's context (as a ``role:"tool"`` message)
right before the next real tool call. It performs no action and touches no
external state — it is a private scratchpad.

This is the well-documented "think tool" pattern: forcing an explicit reasoning
step in each tool-selection step makes a non-reasoning model's tool choices far
more accurate, approximating how native reasoning models operate.

Lifecycle is *system-managed*, not user-managed: the tool is ``hidden`` (so it
never appears in the Settings UI) and the reasoning agent decides whether to
expose it per turn based on the active model's ``supports_reasoning`` capability
flag — see :func:`app.config.model_supports_reasoning` and
``ReasoningAgent.__init__``. Native-reasoning models never see it; models that
lack native reasoning always do.
"""

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Reasoning"


TOOL_CONFIG: ToolConfig = {
    "name": "reasoning",
    "display_name": "Reasoning",
    # System-managed: present in the registry but suppressed from the Settings
    # UI. The reasoning agent gates actual exposure on the model's reasoning
    # capability rather than a per-profile toggle.
    "hidden": True,
}


class ReasoningTool(BuiltInTool):
    name: str = "reasoning"
    description: str = (
        "Think step by step before acting. Call this BEFORE any other tool to "
        "write out your reasoning for the current decision: what the user wants, "
        "what you already know, and which tool you will call next and why. It is a "
        "private scratchpad — it performs no action and simply returns your "
        "reasoning back to you. Do not put the final user-facing answer here."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "Your step-by-step thinking for the current decision."
                ),
            }
        },
        "required": ["reasoning"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        text = str(arguments.get("reasoning") or "").strip()
        logger.info(f"[reasoning] {text[:200]}")
        # Echo the reasoning back so it persists in the model's context (as a
        # tool result) before the next tool call.
        return BuiltInToolResult(
            content=[{"type": "text", "text": text or "(no reasoning provided)"}]
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [ReasoningTool()]
