"""Intrinsic (always-available, hidden) tools.

All intrinsic tools are static and re-registered fresh at every server startup
via :func:`register_intrinsic_tools`. They are not persisted in the
``tools`` table.
"""

from app.tools.intrinsic.base import IntrinsicTool
from app.tools.intrinsic.casual_chat import CasualChatTool
from app.tools.intrinsic.final_answer import FinalAnswerTool
# from app.tools.intrinsic.user_notification import UserNotificationTool
from app.tools.registry import ToolRegistry


__all__ = [
    "IntrinsicTool",
    "CasualChatTool",
    "FinalAnswerTool",
    # "UserNotificationTool",
    "register_intrinsic_tools",
]


def register_intrinsic_tools(registry: ToolRegistry) -> None:
    """Register all built-in intrinsic tools with the registry."""
    for tool_cls in (CasualChatTool, FinalAnswerTool):
        registry.register_intrinsic(tool_cls())
