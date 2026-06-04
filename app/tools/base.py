"""Unified tool abstraction.

All five capability types (intrinsic, built-in, A2A remote agent, MCP server,
skill) implement :class:`Tool`. The reasoning agent dispatches polymorphically
through ``tool.execute(...)`` and consumes the resulting :class:`ToolEvent`
stream uniformly.

Each tool type yields a different *kind* of event stream:

- Intrinsic / Skill : a single ``ToolResultEvent`` (and optional
                      ``ToolBehaviorEvent`` for clarify/terminate/continue).
- A2A / MCP / Builtin : a stream of ``ToolThinkingEvent`` /
                        ``ToolStatusEvent`` updates followed by a final
                        ``ToolResultEvent`` carrying observation parts +
                        token usage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional

from a2a.types import AgentCard, Part


class ToolType(str, Enum):
    INTRINSIC = "intrinsic"
    BUILTIN = "builtin"
    A2A = "a2a"
    MCP = "mcp"
    SKILL = "skill"


class ToolBehavior(str, Enum):
    """Post-execution behavior signal for the reasoning loop.

    The default for "external"-style tools (a2a/mcp/builtin) is OBSERVE: their
    result is fed back as an observation and reasoning continues. Intrinsic
    tools may emit any of the four behaviors.
    """
    CLARIFY = "clarify"      # save context, reset step count, reply to user, end turn
    TERMINATE = "terminate"  # clear context, reply to user, end turn
    CONTINUE = "continue"    # stream content to user, then continue reasoning loop
    OBSERVE = "observe"      # add observation to step history, continue reasoning


@dataclass
class ToolSkill:
    """Lightweight descriptor used in the reasoning prompt's tool listing.

    Equivalent to ``a2a.types.AgentSkill`` but framework-agnostic.
    """
    id: str
    name: str
    description: str
    examples: List[str] = field(default_factory=list)


# ── Event stream emitted by Tool.execute() ─────────────────────────────────


@dataclass
class ToolEvent:
    """Marker base. Concrete subclasses below."""


@dataclass
class ToolThinkingEvent(ToolEvent):
    """Surface a model's chain of thought / tool-call to the UI."""
    text: str
    model_label: Optional[str] = None


@dataclass
class ToolStatusEvent(ToolEvent):
    """In-flight progress / status update (purely informational)."""
    raw: Any


@dataclass
class ToolResultEvent(ToolEvent):
    """Terminal observation produced by the tool.

    Fields
    ------
    observation_text : flattened text fed back into the reasoning prompt
    observation_parts : original A2A Parts (preserved for downstream rendering)
    token_usage : ``{'input_tokens': int, 'output_tokens': int}``
    behavior : the post-execution behavior to apply (default OBSERVE)
    """
    observation_text: str
    observation_parts: List[Part] = field(default_factory=list)
    token_usage: Dict[str, int] = field(default_factory=dict)
    behavior: ToolBehavior = ToolBehavior.OBSERVE


@dataclass
class ToolErrorEvent(ToolEvent):
    """Tool execution failed. ``auth_required=True`` indicates the user must
    authenticate before retrying (``auth_url`` may be a deep link)."""
    message: str
    auth_required: bool = False
    auth_url: Optional[str] = None


# ── Tool base class ────────────────────────────────────────────────────────


class Tool(ABC):
    """Unified base class for all 5 capability types.

    Subclasses must set ``tool_type`` and implement :meth:`execute`. The
    ``tool_id`` is assigned by the registry at registration time.
    """

    tool_type: ToolType

    # Hidden tools are excluded from the UI listing but still
    # available to the reasoning agent (intrinsic tools).
    hidden: bool = False

    def __init__(self) -> None:
        # Populated by ToolRegistry.register_*; safe defaults until then.
        self._tool_id: str = ""

    # ── identity ────────────────────────────────────────────────────────

    @property
    def tool_id(self) -> str:
        return self._tool_id

    @tool_id.setter
    def tool_id(self, value: str) -> None:
        self._tool_id = value

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable display name shown in the UI."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Description shown in the reasoning agent's prompt."""

    @property
    def arguments_schema(self) -> Optional[dict]:
        """Optional JSON Schema for tool arguments. ``None`` means no args."""
        return None

    @property
    def skills(self) -> List[ToolSkill]:
        """Sub-skills exposed in the prompt (default: none)."""
        return []

    # ── prompt & UI helpers ─────────────────────────────────────────────

    def get_card(self) -> Optional[AgentCard]:
        """Return an :class:`AgentCard` for the UI, or ``None``.

        Intrinsic tools usually return ``None`` (they're not user-managed).
        """
        return None

    # ── runtime LLM refresh ────────────────────────────────────────────

    def refresh_llm(self, profile: str) -> None:
        """Re-create the child LLM from current config.

        Called by the reasoning agent *before* reading the model label so
        that ``_model_label_for()`` reflects the latest settings. No-op by
        default; overridden by BuiltInToolGroup and MCPServerTool.
        """

    # ── execution ───────────────────────────────────────────────────────

    @abstractmethod
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
        """Execute the tool and yield :class:`ToolEvent`(s).

        ``arguments`` are the LLM-chosen JSON-Schema args (filtered to the
        tool's schema). ``variables`` are environment-style values from the
        ``variable`` config scope (secrets/keys). ``llm_params`` are
        configuration for the child LLM (provider/model/full_reasoning).
        """
        if False:  # pragma: no cover -- type stub
            yield  # type: ignore[unreachable]
