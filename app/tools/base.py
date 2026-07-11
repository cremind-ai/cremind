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

from a2a.types import Part


class ToolType(str, Enum):
    BUILTIN = "builtin"
    MCP = "mcp"
    SKILL = "skill"


# Separator used to namespace a group's sub-tool ("leaf") into a globally-unique
# native-function name exposed to the model, e.g. ``system_file__overwrite_file``.
LEAF_SEP = "__"


def make_leaf_name(tool_id: str, leaf_name: str) -> str:
    """Build the native-function name the model sees for a group's leaf.

    For single-leaf groups whose leaf is named like the group (e.g.
    ``exec_shell``/``web_search``) the doubled ``exec_shell__exec_shell`` form is
    noise, so collapse it to just the leaf/group name. Multi-leaf groups keep the
    ``<tool_id>__<leaf>`` namespacing (e.g. ``system_file__read_file``). Group
    ``tool_id``s are unique, so single-leaf names can't collide.
    """
    if leaf_name == tool_id:
        return leaf_name
    return f"{tool_id}{LEAF_SEP}{leaf_name}"


@dataclass
class FunctionSpec:
    """One native-function-callable leaf exposed to the reasoning model.

    ``name`` is the namespaced function name the model calls; ``leaf_name`` is
    the original sub-tool name within the owning :class:`Tool`; ``schema`` is the
    full OpenAI tool spec (``{"type": "function", "function": {...}}``) carrying
    the leaf's real JSON-Schema parameters.
    """
    name: str
    leaf_name: str
    schema: Dict[str, Any]


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
    """Marker event emitted while a tool is working (informational, no payload)."""


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

    # locked tools stay in the UI listing (unlike hidden) but their
    # enable/disable toggle is locked on — they cannot be disabled.
    # See ToolConfig.locked.
    locked: bool = False

    # Default enabled state when no ``profile_tools`` row exists — drives both
    # the Setup Wizard's initial toggle and the runtime fallback. Overridden per
    # built-in from ``TOOL_CONFIG["default"]``. See ToolConfig.default.
    default_enabled: bool = True

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

    # ── native function calling ─────────────────────────────────────────

    def leaf_function_specs(
        self,
        *,
        context_id: str,
        profile: str,
        query: str = "",
        arguments: Optional[Dict[str, Any]] = None,
    ) -> List["FunctionSpec"]:
        """Return the native-function specs for this tool's callable leaves.

        The reasoning agent flattens these across all enabled tools into one
        ``tools=`` list for the model and keeps a ``name -> (tool, leaf_name)``
        dispatch map. Default: none. Built-in / MCP groups override to expose
        each sub-tool (with its full JSON-Schema). ``query``/``arguments`` let a
        group run per-request customization (dynamic enums, suppression).
        """
        return []

    async def execute_leaf(
        self,
        *,
        leaf_name: str,
        args: Dict[str, Any],
        context_id: str,
        profile: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
    ) -> AsyncGenerator["ToolEvent", None]:
        """Execute one named leaf with model-chosen ``args`` and yield events.

        Default delegates to :meth:`execute` passing ``leaf_name`` as the query
        (sufficient for single-leaf tools); built-in / MCP groups override to
        dispatch the exact ``(leaf_name, args)`` to their executor.
        """
        async for ev in self.execute(
            query=leaf_name, context_id=context_id, profile=profile,
            arguments={**arguments, **args}, variables=variables,
        ):
            yield ev

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
    ) -> AsyncGenerator[ToolEvent, None]:
        """Execute the tool and yield :class:`ToolEvent`(s).

        ``arguments`` are the LLM-chosen JSON-Schema args (filtered to the
        tool's schema). ``variables`` are environment-style values from the
        ``variable`` config scope (secrets/keys).
        """
        if False:  # pragma: no cover -- type stub
            yield  # type: ignore[unreachable]
