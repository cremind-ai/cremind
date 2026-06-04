"""Registry-facing wrapper for built-in tool groups.

A "built-in tool" in the user-facing sense is a *group* of in-process Python
functions sharing one ``TOOL_CONFIG`` and one child LLM (e.g., the
``Google Calendar`` tool exposes ``list_events`` + ``create_event``). The
group is one row in the ``tools`` table; the individual functions are
implementation details consumed by :class:`BuiltInToolAdapter`.

This module defines :class:`BuiltInToolGroup`, the :class:`Tool` subclass
that the reasoning agent sees. ``execute()`` delegates to the existing
``BuiltInToolAdapter`` (which spawns the child LLM and runs the functions
in-process), then collapses the synthetic A2A event stream into a single
:class:`ToolResultEvent`.
"""

from __future__ import annotations

import importlib
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)

from app.config.settings import BaseConfig
from app.lib.llm.base import LLMProvider
from app.tools.base import (
    Tool,
    ToolEvent,
    ToolErrorEvent,
    ToolResultEvent,
    ToolSkill,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.tools.builtin.adapter import BuiltInToolAdapter
from app.tools.builtin.base import BuiltInTool as BuiltInFunction
from app.utils.event_parser import parse_agent_events
from app.utils.logger import logger


class BuiltInToolGroup(Tool):
    """One built-in tool group (== one ``TOOL_CONFIG``, one ``app.tools.builtin.*`` module).

    Owns a child :class:`LLMProvider` and a list of inner :class:`BuiltInFunction`
    instances. ``execute()`` runs the LLM with the functions exposed as tools
    and yields a uniform :class:`ToolEvent` stream.
    """

    tool_type = ToolType.BUILTIN

    def __init__(
        self,
        *,
        config_name: str,           # module stem e.g. "weather", "gg_calendar"
        display_name: str,          # SERVER_NAME
        description: str,
        functions: List[BuiltInFunction],
        llm: Optional[LLMProvider] = None,
        arguments_schema: Optional[dict] = None,
        oauth_provider: Optional[Callable[[str], Any]] = None,
        prepare_tools: Optional[Callable[[str, list], list]] = None,
        full_reasoning: bool = False,
        system_prompt: Optional[str] = None,
        tool_instructions: Optional[str] = None,
        llm_factory: Optional[Callable[[str, str], LLMProvider]] = None,
        direct_dispatch: bool = False,
    ):
        super().__init__()
        self._config_name = config_name
        self._display_name = display_name
        self._description = description
        self._arguments_schema = arguments_schema
        self._llm_factory = llm_factory
        self._adapter = BuiltInToolAdapter(
            tools=functions,
            llm=llm,
            mcp_auth=None,  # set lazily via apply_oauth
            description=description,
            name=display_name,
            system_prompt=system_prompt,
            tool_instructions=tool_instructions,
            prepare_tools=prepare_tools,
            full_reasoning=full_reasoning,
            direct_dispatch=direct_dispatch,
        )
        self._oauth_provider = oauth_provider
        if oauth_provider is not None:
            try:
                self._adapter._mcp_auth = oauth_provider(self._adapter)
            except Exception:  # noqa: BLE001
                logger.exception(f"Failed to attach OAuth client to '{display_name}'")

    # ── identity ────────────────────────────────────────────────────────

    @property
    def config_name(self) -> str:
        return self._config_name

    @property
    def name(self) -> str:
        return self._display_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def arguments_schema(self) -> Optional[dict]:
        return self._arguments_schema

    @property
    def adapter(self) -> BuiltInToolAdapter:
        return self._adapter

    @property
    def is_llm_bound(self) -> bool:
        """True once a child LLM has been attached to the adapter.

        Built-in tools register at startup even before the setup wizard runs;
        in that pre-setup window the adapter has no LLM, so ``execute()`` will
        return a "no LLM configured" error event. The frontend uses this flag
        to render a "Setup required" badge.
        """
        return self._adapter._llm is not None

    @property
    def skills(self) -> List[ToolSkill]:
        return [
            ToolSkill(id=f.name, name=f.name, description=f.description or f.name)
            for f in self._adapter._tools
        ]

    def get_card(self) -> AgentCard:
        return self._adapter.create_synthetic_card()

    # ── runtime LLM refresh ────────────────────────────────────────────

    def refresh_llm(self, profile: str) -> None:
        """Re-create the child LLM from current config (called before model-label read)."""
        if self._llm_factory:
            try:
                llm = self._llm_factory(self.tool_id or self._config_name, profile)
                self._adapter.update_config(llm=llm)
            except Exception:  # noqa: BLE001
                pass  # keep current LLM; adapter handles the "no LLM" case

    # ── runtime updates ─────────────────────────────────────────────────

    def update_runtime_config(
        self,
        *,
        llm: Optional[LLMProvider] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        full_reasoning: Optional[bool] = None,
    ) -> None:
        """Update the underlying adapter's runtime knobs."""
        self._adapter.update_config(
            llm=llm,
            system_prompt=system_prompt,
            description=description,
            full_reasoning=full_reasoning,
        )
        if description is not None and description:
            self._description = description

    # ── execution ───────────────────────────────────────────────────────

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
        # Refresh the child LLM so config changes take effect without restart.
        # (Also called by the reasoning agent before reading Model_Label.)
        self.refresh_llm(profile)

        # Apply per-call llm_params (e.g., full_reasoning override)
        if "full_reasoning" in llm_params:
            self._adapter.update_config(full_reasoning=bool(llm_params["full_reasoning"]))

        # Yield a thinking event for UI continuity
        yield ToolThinkingEvent(
            text=f"[{self._display_name}] {query}",
            model_label=getattr(self._adapter._llm, "model_label", None) if self._adapter._llm else None,
        )

        events: list = []
        metadata: dict = {}
        if arguments:
            metadata["arguments"] = arguments
        if variables:
            metadata["variables"] = variables

        try:
            async for ev in self._adapter.request(
                query=query, context_id=context_id, metadata=metadata, profile=profile,
            ):
                events.append(ev)
                yield ToolStatusEvent(raw=ev)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Built-in tool '{self._display_name}' failed")
            yield ToolErrorEvent(message=str(e))
            return

        # Collapse the A2A-shaped event stream into a single ToolResultEvent
        observation_text, token_usage, observation_parts = parse_agent_events(events)
        yield ToolResultEvent(
            observation_text=observation_text,
            observation_parts=observation_parts,
            token_usage=token_usage or {},
        )


# ── module loader ──────────────────────────────────────────────────────────


def load_builtin_tool_module(name: str):
    """Import the built-in tool module ``app.tools.builtin.{name}``."""
    return importlib.import_module(f"app.tools.builtin.{name}")
