"""Skill wrapped as a unified :class:`Tool`.

Skills use the reasoning agent's own LLM (no child LLM is spawned). When
invoked, a skill yields a single :class:`ToolResultEvent` carrying the full
``SKILL.md`` content as the observation; the reasoning agent then continues
following the instructions inside the skill.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, Optional

from a2a.types import (
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
    def environment_variables(self) -> list[dict[str, Any]]:
        """Environment-variable specs declared in the SKILL.md frontmatter.

        Declared via ``metadata.environment_variables`` as a list of objects,
        each describing one variable's config/UI metadata so the frontend can
        render it accurately::

            environment_variables: [
              {"name": "HA_URL", "required": true, "description": "..."},
              {"name": "HA_TOKEN", "required": false, "secret": true, ...},
            ]

        Each entry is normalized to a dict with keys: ``name`` (str),
        ``description`` (str), ``required`` (bool), ``secret`` (bool | None —
        ``None`` lets the API layer fall back to its name-based heuristic),
        ``type`` (str), ``default`` (str | None) and ``enum`` (list[str]).

        A plain string entry is still accepted and treated as an optional
        string variable, so legacy manifests keep working. Entries without a
        usable ``name`` are dropped. Returns an empty list when none declared.
        """
        raw = self._info.metadata.get("environment_variables") or []
        if not isinstance(raw, list):
            return []
        specs: list[dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, str) and entry:
                specs.append(self._normalize_env_spec({"name": entry}))
            elif isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    specs.append(self._normalize_env_spec(entry))
        return specs

    @staticmethod
    def _normalize_env_spec(entry: dict[str, Any]) -> dict[str, Any]:
        """Coerce one raw frontmatter entry into a normalized env-var spec."""
        name = entry["name"]
        desc = entry.get("description")
        default = entry.get("default")
        enum_raw = entry.get("enum")
        return {
            "name": name,
            "description": str(desc) if desc else name,
            "required": bool(entry.get("required", False)),
            # None => caller applies the secret-name heuristic.
            "secret": bool(entry["secret"]) if "secret" in entry else None,
            "type": str(entry.get("type") or "string"),
            "default": str(default) if default is not None else None,
            "enum": [str(v) for v in enum_raw] if isinstance(enum_raw, list) else [],
        }

    @property
    def environment_variable_names(self) -> list[str]:
        """Declared variable names only (order-preserving).

        Convenience for callers that just need the names — e.g. writing the
        skill's ``.env`` file — without the per-variable UI metadata.
        """
        return [spec["name"] for spec in self.environment_variables]

    async def execute(
        self,
        *,
        query: str,
        context_id: str,
        profile: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
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
