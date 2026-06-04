"""LLM provider factory for creating provider instances by name.

Used to create per-MCP-server LLM providers from stored configuration.
Supports reading API keys from DynamicConfigStorage (SQLite) with env fallback.
"""

import json
from typing import Optional

from app.config import load_provider_catalog
from app.config.settings import BaseConfig
from .base import LLMProvider

# Providers with dedicated Python SDK implementations
_DEDICATED_PROVIDERS = {"anthropic", "github-copilot", "groq", "openai", "ollama", "vertexai", "vllm"}

# All providers listed here appear in the UI.  Providers not in
# ``_DEDICATED_PROVIDERS`` are handled as generic OpenAI-compatible
# endpoints using ``base_url`` from their TOML catalog.
SUPPORTED_LLM_PROVIDERS = [
    "anthropic",
    "chutes",
    "cloudflare-ai-gateway",
    "deepseek",
    "fireworks",
    "github-copilot",
    "google-gemini",
    "groq",
    "huggingface",
    "litellm",
    "minimax",
    "mistral",
    "moonshot",
    "nvidia",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "qwen",
    "together",
    "vertexai",
    "vllm",
    "xai",
]


def _get_api_key(provider_name: str, config_storage=None, profile: str | None = None) -> str:
    """Get API key for a provider from config storage or env fallback."""
    if config_storage is not None:
        kwargs = {"profile": profile} if profile is not None else {}
        val = config_storage.get("llm_config", f"{provider_name}.api_key", **kwargs)
        if val:
            return val
    return BaseConfig.get_provider_api_key(provider_name, profile=profile)


def _get_auth_method(provider_name: str, config_storage=None, profile: str | None = None) -> str | None:
    """Get the active auth method for a provider from config storage."""
    if config_storage is not None:
        kwargs = {"profile": profile} if profile is not None else {}
        return config_storage.get("llm_config", f"{provider_name}.auth_method", **kwargs)
    return None


def _get_config_value(provider_name: str, key: str, config_storage=None, profile: str | None = None) -> str | None:
    """Get a provider config value from storage."""
    if config_storage is not None:
        kwargs = {"profile": profile} if profile is not None else {}
        return config_storage.get("llm_config", f"{provider_name}.{key}", **kwargs)
    return None


def create_llm_provider(
    provider_name: str,
    model_name: Optional[str] = None,
    config_storage=None,
    profile: str | None = None,
    default_reasoning_effort: Optional[str] = None,
) -> LLMProvider:
    """Create an LLMProvider instance from a provider name and model.

    Args:
        provider_name: One of "groq", "openai", "ollama", "vertexai", "vllm"
        model_name: Model identifier. Defaults to BaseConfig.REASONING_MODEL_NAME.
        config_storage: Optional DynamicConfigStorage for reading API keys from SQLite.
        profile: Profile name for scoped config lookup. Defaults to "admin" in storage.

    Returns:
        An LLMProvider instance.

    Raises:
        ValueError: If provider_name is unknown or required credentials are missing.
    """
    model = model_name or BaseConfig.REASONING_MODEL_NAME
    if not model:
        raise ValueError(
            "No model name provided and no default model configured. "
            "Ensure the 'low' model group is set via the setup wizard."
        )
    # Strip provider prefix if model string starts with "provider_name/"
    # (UI sends "provider/model_id" format, e.g. "groq/openai/gpt-oss-120b")
    if model and provider_name and model.startswith(f"{provider_name}/"):
        model = model[len(provider_name) + 1:]
    _kw = {"profile": profile} if profile is not None else {}

    provider: LLMProvider
    auth_method = _get_auth_method(provider_name, config_storage, profile=profile)

    if provider_name == "anthropic":
        from .anthropic import AnthropicLLMProvider

        if auth_method == "setup_token":
            token = _get_config_value("anthropic", "setup_token", config_storage, profile=profile)
            if not token:
                raise ValueError("Anthropic setup token is not configured")
            provider = AnthropicLLMProvider(
                bearer_token=token, model_name=model, default_reasoning_effort=default_reasoning_effort,
            )
        else:
            api_key = _get_api_key("anthropic", config_storage, profile=profile)
            if not api_key:
                raise ValueError("Anthropic API key is not configured")
            provider = AnthropicLLMProvider(
                api_key=api_key, model_name=model, default_reasoning_effort=default_reasoning_effort,
            )

    elif provider_name == "groq":
        api_key = _get_api_key("groq", config_storage, profile=profile)
        if not api_key:
            raise ValueError("Groq API key is not configured")
        from .groq import GroqLLMProvider
        provider = GroqLLMProvider(api_key=api_key, model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "openai":
        if auth_method == "codex_oauth":
            api_key = _get_config_value("openai", "oauth_token", config_storage, profile=profile)
            if not api_key:
                raise ValueError("OpenAI OAuth token is not configured")
        else:
            api_key = _get_api_key("openai", config_storage, profile=profile)
            if not api_key:
                raise ValueError("OpenAI API key is not configured")
        from .openai import OpenAILLMProvider
        provider = OpenAILLMProvider(api_key=api_key, model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "ollama":
        from .ollama import OllamaLLMProvider
        provider = OllamaLLMProvider(api_key="ollama", model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "vertexai":
        service_account = None
        if config_storage:
            service_account = config_storage.get("llm_config", "vertexai.service_account", **_kw)
        if not service_account:
            raise ValueError("VertexAI service account is not configured")

        credentials = json.loads(service_account)

        project_id = None
        if config_storage:
            project_id = config_storage.get("llm_config", "vertexai.project_id", **_kw)

        location = None
        if config_storage:
            location = config_storage.get("llm_config", "vertexai.location", **_kw)
        if not location:
            location = "global"

        from .vertexai import VertexAILLMProvider
        provider = VertexAILLMProvider(
            credentials=credentials,
            project_id=project_id,
            location=location,
            model_name=model,
            default_reasoning_effort=default_reasoning_effort,
        )

    elif provider_name == "vllm":
        from .vllm import VllmLLMProvider
        provider = VllmLLMProvider(api_key="vllm", model_name=model, default_reasoning_effort=default_reasoning_effort)

    elif provider_name == "github-copilot":
        oauth_token = _get_api_key("github-copilot", config_storage, profile=profile)
        if not oauth_token:
            raise ValueError("GitHub Copilot credentials are not configured")
        from .github_copilot import GitHubCopilotLLMProvider
        provider = GitHubCopilotLLMProvider(
            oauth_token=oauth_token,
            model_name=model,
            default_reasoning_effort=default_reasoning_effort,
        )

    else:
        # Generic OpenAI-compatible provider — uses base_url from its TOML catalog
        catalog = load_provider_catalog(provider_name)
        if not catalog:
            raise ValueError(
                f"Unknown LLM provider '{provider_name}'. "
                f"Supported: {', '.join(SUPPORTED_LLM_PROVIDERS)}"
            )

        base_url = catalog.get("provider", {}).get("base_url")

        # Region variants: use region-specific endpoints
        if provider_name == "qwen" and auth_method == "api_key_cn":
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        elif provider_name == "minimax" and auth_method == "api_key_cn":
            base_url = "https://api.minimaxi.com/v1"
        elif provider_name == "moonshot" and auth_method == "api_key_cn":
            base_url = "https://api.moonshot.cn/v1"

        # Cloudflare AI Gateway: construct base_url from account_id + gateway_id
        if provider_name == "cloudflare-ai-gateway":
            account_id = _get_config_value(provider_name, "account_id", config_storage, profile=profile)
            gateway_id = _get_config_value(provider_name, "gateway_id", config_storage, profile=profile)
            if account_id and gateway_id:
                base_url = f"https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway_id}/openai"

        # Allow overriding base_url from config storage (e.g. LiteLLM base_url)
        stored_base_url = _get_config_value(provider_name, "base_url", config_storage, profile=profile)
        if stored_base_url:
            base_url = stored_base_url

        # Resolve API key / token based on auth method
        api_key = None
        if auth_method and auth_method != "api_key":
            # Try to read the first secret field defined for this auth method
            from app.config.provider_auth import normalize_provider_auth_methods
            for am in normalize_provider_auth_methods(catalog.get("provider", {})):
                if am["id"] == auth_method:
                    for field_key, field_spec in am.get("fields", {}).items():
                        if field_spec.get("secret", False):
                            val = _get_config_value(provider_name, field_key, config_storage, profile=profile)
                            if val:
                                api_key = val
                                break
                    break
        if not api_key:
            api_key = _get_api_key(provider_name, config_storage, profile=profile)

        if not api_key:
            display = catalog.get("provider", {}).get("display_name", provider_name)
            raise ValueError(f"{display} credentials are not configured")

        from .openai import OpenAILLMProvider
        provider = OpenAILLMProvider(
            api_key=api_key, model_name=model, base_url=base_url,
            default_reasoning_effort=default_reasoning_effort,
        )

    provider.provider_name = provider_name
    return provider
