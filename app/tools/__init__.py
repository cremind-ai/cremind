"""Unified tool framework.

Public exports:
- :class:`Tool`                    -- common interface for all 5 capability types
- :class:`ToolType`                -- enum of {intrinsic, builtin, a2a, mcp, skill}
- :class:`ToolBehavior`            -- post-execution behavior signal
- :class:`ToolRegistry`            -- single source of truth for tool_ids
- :class:`ToolConfigManager`       -- per-profile per-tool scoped config
- :class:`ToolEvent` and friends   -- unified execution event stream
"""

from app.tools.base import (
    Tool,
    ToolBehavior,
    ToolEvent,
    ToolErrorEvent,
    ToolResultEvent,
    ToolSkill,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.tools.config_manager import ToolConfigManager
from app.tools.ids import (
    ToolIdConflictError,
    allocate_fixed_tool_id,
    allocate_unique_tool_id,
    slugify,
)
from app.tools.registry import (
    ToolRegistry,
    get_tool_registry,
    set_tool_registry,
)

__all__ = [
    "Tool",
    "ToolType",
    "ToolBehavior",
    "ToolEvent",
    "ToolThinkingEvent",
    "ToolStatusEvent",
    "ToolResultEvent",
    "ToolErrorEvent",
    "ToolSkill",
    "ToolConfigManager",
    "ToolRegistry",
    "ToolIdConflictError",
    "get_tool_registry",
    "set_tool_registry",
    "slugify",
    "allocate_fixed_tool_id",
    "allocate_unique_tool_id",
]
