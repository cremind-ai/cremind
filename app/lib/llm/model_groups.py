"""LLM Model Group Manager.

The agent reasons on a **single configured model** (the ``high`` group, stored
under ``model_group.high``); ``create_llm_for_model`` is the canonical accessor.
``main`` is an alias for it.

Three **optional** auxiliary groups fall back to the single model when unset:

- ``vision`` — used only by the ``image_understanding`` tool.
- ``low`` — the low-performance / cheap model for lightweight auxiliary tasks
  (e.g. the skill-event matching gate and the ``documentation_search`` relevance
  judge). Generalized so future features needing a cheaper model can resolve it
  via ``create_llm_for_group("low", ...)``.
- ``plan`` — the model used during plan mode's *planning* phase (research,
  clarifying questions, writing the plan for approval, and after a cancel). The
  agent switches back to the single model once the plan is accepted and
  execution begins. Resolved via ``create_llm_for_group("plan", ...)``.
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

        ``high`` / ``main`` resolve to the single configured reasoning model
        (stored under ``model_group.high``). ``low`` and ``vision`` resolve their
        own optional keys, each falling back to the single model when unset.
        Priority: SQLite override > TOML default.
        """
        # ``main`` is an alias for the single configured reasoning model.
        if group in ("high", "main"):
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

        # The dedicated "vision", "low", and "plan" groups are optional: when the
        # user hasn't picked one they transparently fall back to the "high" group,
        # so the dependent feature works out of the box. ``vision`` on a non-vision
        # model still surfaces a clear error at use time; ``low`` (the
        # low-performance / cheap auxiliary model) and ``plan`` (the plan-mode
        # planning model) just run on the main model.
        if not group_value and group in ("vision", "low", "plan"):
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

        Per-tool model overrides were removed. Two tools are special-cased:

        ``image_understanding`` — the Specialized Vision Model toggle decides
        *which* model runs it:

        - feature ON  → the dedicated ``vision`` model (which itself falls back to
          the single model when the user hasn't picked one).
        - feature OFF → the single configured (main) model directly. We bypass the
          ``vision`` group here so a *stale* dedicated model the user configured and
          then turned off can't leak back in.

        ``documentation_search`` — its relevance judge is a frugal LLM-as-judge, so
        it runs on the ``low`` (low-performance) model, which itself falls back to
        the single model when unset.

        Every other tool uses the single configured model. ``tool_name`` may be a
        module name or a slug — both contain the relevant substring for that tool.
        """
        name = tool_name or ""
        if "image_understanding" in name:
            from app.config import vision_feature_enabled
            if vision_feature_enabled(profile):
                return self.create_llm_for_group("vision", profile=profile)
            return self.create_llm_for_model(profile=profile)
        # The documentation_search relevance judge is a lightweight LLM-as-judge
        # (name+description only, structured tool-call output, no bodies/history) —
        # exactly the cheap auxiliary task the low-performance group exists for.
        # Falls back to the single model when ``low`` is unset.
        if "documentation_search" in name:
            return self.create_llm_for_group("low", profile=profile)
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
        return model_supports_vision(provider, model, profile=profile)

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
