"""LLM Model Group Manager.

Resolves model group names ('high', 'low') to concrete provider+model pairs.
Supports per-tool model overrides and custom model selections.
"""

from typing import Optional

from app.config import settings as dynaconf_settings
from app.tools.builtin import get_builtin_tool_config
from app.storage.dynamic_config_storage import DynamicConfigStorage
from .base import LLMProvider
from .exceptions import SetupRequiredError
from .factory import create_llm_provider


class ModelGroupManager:
    """Manages model group resolution and LLM provider creation."""

    def __init__(self, config_storage: DynamicConfigStorage):
        self.config_storage = config_storage

    def get_provider_and_model(self, group: str, profile: str | None = None) -> tuple[str, str]:
        """Resolve a model group ('high' or 'low') to (provider_name, model_name).

        The group value format is 'provider/model_name' (e.g., 'groq/openai/gpt-oss-120b').
        The first segment before '/' is the provider, the rest is the model identifier.

        Priority: SQLite override > TOML default.
        """
        # Try SQLite first
        _kw = {"profile": profile} if profile is not None else {}
        group_value = self.config_storage.get("llm_config", f"model_group.{group}", **_kw)

        # Fall back to TOML
        if not group_value:
            try:
                group_value = dynaconf_settings.get(f"llm.model_groups.{group}")
            except Exception:
                pass

        if not group_value:
            raise SetupRequiredError(
                (
                    f"The '{group}' model group is not configured. "
                    f"Open Settings → LLM Providers and choose a model "
                    f"for the {group.capitalize()} group."
                ),
                code="missing_model_group",
                settings_path="/settings/llm",
                settings_label="LLM Providers",
            )

        return self._parse_group_value(group_value)

    def get_default_provider(self, profile: str | None = None) -> str:
        """Get the default LLM provider name."""
        _kw = {"profile": profile} if profile is not None else {}
        val = self.config_storage.get("llm_config", "default_provider", **_kw)
        if val:
            return val
        try:
            return dynaconf_settings.get("llm.default_provider", "")
        except Exception:
            return ""

    def create_llm_for_group(self, group: str, profile: str | None = None) -> LLMProvider:
        """Create an LLMProvider instance for the given model group."""
        provider_name, model_name = self.get_provider_and_model(group, profile=profile)
        reasoning_effort = self._get_group_reasoning_effort(group, profile=profile)
        return create_llm_provider(
            provider_name, model_name,
            config_storage=self.config_storage, profile=profile,
            default_reasoning_effort=reasoning_effort,
        )

    def create_llm_for_tool(self, tool_name: str, profile: str | None = None) -> LLMProvider:
        """Create LLM for a specific tool.

        Priority:
        1. Tool-level model override in SQLite (tool_configs: tool_name, 'llm_model')
        2. Tool's default_model_group from its built-in TOOL_CONFIG
        3. 'low' group as ultimate fallback
        """
        # Check for tool-level override
        _kw = {"profile": profile} if profile is not None else {}
        tool_model = self.config_storage.get_tool_config(tool_name, "llm_model", **_kw)
        if tool_model:
            provider_name, model_name = self._parse_group_value(tool_model)
            return create_llm_provider(
                provider_name, model_name,
                config_storage=self.config_storage, profile=profile,
            )

        # Check tool's default model group from its inline TOOL_CONFIG
        tool_schema = get_builtin_tool_config(tool_name)
        tool_info = tool_schema.get("tool", {})
        default_group = tool_info.get("default_model_group", "low")

        return self.create_llm_for_group(default_group, profile=profile)

    def _get_group_reasoning_effort(self, group: str, profile: str | None = None) -> Optional[str]:
        """Look up the user's selected reasoning_effort for a model group from SQLite."""
        _kw = {"profile": profile} if profile is not None else {}
        return self.config_storage.get("llm_config", f"model_group.{group}.reasoning_effort", **_kw) or None

    @staticmethod
    def _parse_group_value(group_value: str) -> tuple[str, str]:
        """Parse a group value like 'groq/openai/gpt-oss-120b' into (provider, model).

        The first segment is the provider name, the rest is the model identifier.
        """
        parts = group_value.split("/", 1)
        if len(parts) == 1:
            return parts[0], parts[0]
        return parts[0], parts[1]
