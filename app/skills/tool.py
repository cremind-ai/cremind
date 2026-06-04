"""Skill wrapped as a unified :class:`Tool`.

Skills use the reasoning agent's own LLM (no child LLM is spawned). When
invoked, a skill yields a single :class:`ToolResultEvent` carrying the full
``SKILL.md`` content as the observation; the reasoning agent then continues
following the instructions inside the skill.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, Optional

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    Part,
    TextPart,
)

from app.tools.base import (
    Tool,
    ToolBehavior,
    ToolEvent,
    ToolResultEvent,
    ToolType,
)
from app.skills.scanner import SkillInfo


class SkillTool(Tool):
    """Wraps a parsed :class:`SkillInfo` as a registry :class:`Tool`."""

    tool_type = ToolType.SKILL

    def __init__(self, skill_info: SkillInfo):
        super().__init__()
        self._info = skill_info
        self._source = str(skill_info.dir_path)

    @property
    def info(self) -> SkillInfo:
        return self._info

    @property
    def source(self) -> str:
        return self._source

    @property
    def name(self) -> str:
        return self._info.name

    @property
    def description(self) -> str:
        return self._info.description

    @property
    def environment_variables(self) -> list[str]:
        """Names of environment variables declared in the SKILL.md frontmatter.

        Declared via ``metadata.environment_variables`` as a list of strings.
        Returns an empty list when none are declared.
        """
        raw = self._info.metadata.get("environment_variables") or []
        if not isinstance(raw, list):
            return []
        return [v for v in raw if isinstance(v, str) and v]

    def get_card(self) -> AgentCard:
        return AgentCard(
            name=self._info.name,
            description=self._info.description,
            url=f"skill://{self._info.name}",
            version="1.0.0",
            defaultInputModes=["text"],
            defaultOutputModes=["text"],
            capabilities=AgentCapabilities(streaming=True),
            skills=[],
        )

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
        content = self._info.full_content
        if not content:
            text = f"Skill '{self._info.name}' has no content."
        else:
            skill_md_path = self._info.dir_path / "SKILL.md"
            text = f"[Skill loaded from {skill_md_path}]\n\n{content}"
        yield ToolResultEvent(
            observation_text=text,
            observation_parts=[Part(root=TextPart(text=text))],
            token_usage={},
            behavior=ToolBehavior.OBSERVE,
        )
