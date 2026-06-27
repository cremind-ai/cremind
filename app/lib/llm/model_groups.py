"""LLM Model Group Manager.

The agent runs on a **single configured model**. Historically there were two
model "groups" (``high`` for reasoning, ``low`` for the inner tool-routing LLM);
the routing LLM was removed when the agent moved to native function calling, so
``high`` and ``low`` now resolve to the *same* model. The single model is still
stored under the ``model_group.high`` key (kept to avoid a config migration);
``create_llm_for_model`` is the canonical accessor.

A separate **optional** ``vision`` model remains, used only by the
``image_understanding`` tool. When unset it falls back to the single model.
"""

from typing import Optional

from app.config import settings as dynaconf_settings
from app.storage.dynamic_config_storage import DynamicConfigStorage
from .base import LLMProvider
from .exceptions import SetupRequiredError
from .factory import create_llm_provider


class ModelGroupManager:
    """Manages model group resolution and LLM provider creation."""

    def __init__(self, config_storage: DynamicConfigStorage):
        self.config_storage = config_storage

    def get_provider_and_model(self, group: str, profile: str | None = None) -> tuple[str, str]:
        """Resolve a model group to (provider_name, model_name).

        The group value format is 'provider/model_name' (e.g., 'groq/openai/gpt-oss-120b').
        The first segment before '/' is the provider, the rest is the model identifier.

        ``high`` / ``low`` / ``main`` all resolve to the single configured model
        (stored under ``model_group.high``). ``vision`` resolves its own optional
        key, falling back to the single model. Priority: SQLite override > TOML default.
        """
        # Collapse the legacy high/low split: there is one model now. Any caller
        # asking for the reasoning ("high"), the old routing ("low"), or the
        # canonical ("main") group gets the single configured model.
        if group in ("high", "low", "main"):
            group = "high"

        # Try SQLite first
        _kw = {"profile": profile} if profile is not None else {}
        group_value = self.config_storage.get("llm_config", f"model_group.{group}", **_kw)

        # Fall back to TOML
        if not group_value:
            try:
                group_value = dynaconf_settings.get(f"llm.model_groups.{group}")
            except Exception:
                pass

        # The dedicated "vision" group is optional: when the user hasn't picked
        # a vision model it transparently falls back to the "high" group, so
        # image understanding works out of the box on a vision-capable high
        # model. A non-vision model still surfaces a clear error at use time.
        if not group_value and group == "vision":
            return self.get_provider_and_model("high", profile=profile)

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
        """Create an LLMProvider instance for the given model group.

        ``high``/``low``/``main`` collapse to the single configured model;
        ``vision`` resolves the optional vision model (falling back to the
        single model). Prefer :meth:`create_llm_for_model` for the single model.
        """
        provider_name, model_name = self.get_provider_and_model(group, profile=profile)
        reasoning_effort = self._get_group_reasoning_effort(group, profile=profile)
        return create_llm_provider(
            provider_name, model_name,
            config_storage=self.config_storage, profile=profile,
            default_reasoning_effort=reasoning_effort,
        )

    def create_llm_for_model(self, profile: str | None = None) -> LLMProvider:
        """Create the single configured model used for reasoning, generation,
        and all auxiliary tasks (compaction, summaries, tool-internal LLM calls)."""
        return self.create_llm_for_group("high", profile=profile)

    def create_llm_for_tool(self, tool_name: str, profile: str | None = None) -> LLMProvider:
        """Create the child LLM for a tool's internal LLM step.

        Per-tool model overrides were removed. ``image_understanding`` is the only
        special case, and the Specialized Vision Model toggle decides *which* model
        runs it:

        - feature ON  → the dedicated ``vision`` model (which itself falls back to
          the single model when the user hasn't picked one).
        - feature OFF → the single configured (main) model directly. We bypass the
          ``vision`` group here so a *stale* dedicated model the user configured and
          then turned off can't leak back in.

        Every other tool uses the single configured model. ``tool_name`` may be a
        module name or a slug — both contain ``image_understanding`` for that tool.
        """
        if "image_understanding" in (tool_name or ""):
            from app.config import vision_feature_enabled
            if vision_feature_enabled(profile):
                return self.create_llm_for_group("vision", profile=profile)
            return self.create_llm_for_model(profile=profile)
        return self.create_llm_for_model(profile=profile)

    def image_understanding_available(self, profile: str | None = None) -> bool:
        """Whether the ``image_understanding`` tool should be exposed for ``profile``.

        Image understanding reaches a model only through this tool, so the tool is
        available whenever the model that *would* run it can see images:

        - feature ON  → always (a dedicated vision model handles images; a
          non-vision choice surfaces a clean runtime error rather than silently
          missing).
        - feature OFF → only when the main model itself supports vision (it runs
          image understanding directly).

        Hidden only when the feature is off *and* the main model is text-only.
        """
        from app.config import model_supports_vision, vision_feature_enabled
        if vision_feature_enabled(profile):
            return True
        try:
            provider, model = self.get_provider_and_model("high", profile=profile)
        except Exception:  # noqa: BLE001 — unconfigured/missing model → treat as unavailable
            return False
        return model_supports_vision(provider, model)

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
