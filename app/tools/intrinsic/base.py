"""Base class for intrinsic tools.

Intrinsic tools share the reasoning agent's LLM (no child LLM is spawned).
They take ``action_input`` from the reasoning step and produce a
:class:`ToolResultEvent` plus a :class:`ToolBehaviorEvent` indicating how the
reasoning loop should react (clarify, terminate, continue, or observe).
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, AsyncGenerator, Dict

from a2a.types import Part, TextPart

from app.tools.base import (
    Tool,
    ToolBehavior,
    ToolEvent,
    ToolResultEvent,
    ToolType,
)


class IntrinsicTool(Tool):
    """Base class for intrinsic tools (always available, hidden from UI)."""

    tool_type = ToolType.INTRINSIC
    hidden = True

    @property
    @abstractmethod
    def behavior(self) -> ToolBehavior:
        """Post-execution behavior. Subclasses must declare this."""

    async def produce_text(self, arguments: Dict[str, Any]) -> str:
        """Return the text payload for this invocation.

        Default implementation returns the ``action_input`` argument verbatim
        (which is the existing pass-through behavior). Skill-style intrinsic
        tools override this to supply skill content.
        """
        return arguments.get("action_input", "") or ""

    async def execute(
        self,
        *,
        query: str,
        context_id: str,
        profile: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
        llm_params: Dict[str, Any],
    ) -> AsyncGenerator[ToolEvent, None]:
        # Intrinsic tools accept ``action_input`` either in arguments or as ``query``.
        merged_args = dict(arguments)
        merged_args.setdefault("action_input", query)
        text = await self.produce_text(merged_args)
        yield ToolResultEvent(
            observation_text=text,
            observation_parts=[Part(root=TextPart(text=text))] if text else [],
            token_usage={},
            behavior=self.behavior,
        )
