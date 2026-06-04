"""Casual chat intrinsic tool (CLARIFY behavior)."""

from app.tools.base import ToolBehavior
from app.tools.intrinsic.base import IntrinsicTool


class CasualChatTool(IntrinsicTool):
    """Engage in general conversation; conversation continues, step count resets."""

    @property
    def name(self) -> str:
        return "Casual Chat Tool"

    @property
    def description(self) -> str:
        return (
            "A casual chat tool that can engage in general conversation and "
            "provide information on various topics. Make sure to provide the "
            "content to be sent to user in the Action_Input field when using this tool."
        )

    @property
    def behavior(self) -> ToolBehavior:
        return ToolBehavior.CLARIFY
