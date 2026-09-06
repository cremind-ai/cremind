"""Unit tests for ``model_supports_reasoning`` (the native-reasoning capability
flag that gates the ``reasoning`` think-tool).

Exercises the real provider catalogs in ``app/config/providers/*.toml`` so these
also act as a guard that the ``supports_reasoning`` flags parse and are set on
the models we expect.
"""

from __future__ import annotations

from app.config import load_provider_catalog, model_supports_reasoning


def _assert_catalogued(provider: str, model: str) -> None:
    """Fail loudly if ``model`` is no longer in ``provider``'s catalog.

    ``model_supports_reasoning`` returns False for *unknown* models too, so a
    False-case naming a model a catalog refresh has dropped keeps passing while
    proving nothing. Every case below is pinned to an id the TOML really carries.
    """
    ids = {m.get("id") for m in (load_provider_catalog(provider).get("models") or [])}
    assert model in ids, f"{provider}/{model} is no longer in the provider catalog"


def test_reasoning_model_flagged_true():
    # GPT-6 / GPT-5.x are reasoning models; Claude 4.x supports extended thinking.
    for provider, model in [
        ("openai", "gpt-6-astra"),
        ("openai", "gpt-5.4"),
        ("anthropic", "claude-opus-4-7"),
        ("xai", "grok-3-mini"),
    ]:
        _assert_catalogued(provider, model)
        assert model_supports_reasoning(provider, model) is True


def test_non_reasoning_model_flagged_false():
    # Plain chat models across three provider families. Note there is no
    # Anthropic case: every model in anthropic.toml supports extended thinking,
    # so the old ``claude-3-5-haiku-20241022`` assertion could only be repointed
    # out of that catalog (Mistral Large stands in as the other-vendor case).
    # Same for vertexai/google-gemini — every Gemini entry is reasoning-capable.
    for provider, model in [
        ("openai", "gpt-4.1"),
        ("mistral", "mistral-large-latest"),
        ("groq", "llama-3.3-70b-versatile"),
    ]:
        _assert_catalogued(provider, model)
        assert model_supports_reasoning(provider, model) is False


def test_provider_prefix_is_stripped():
    # The model id may arrive prefixed with ``<provider>/``.
    assert model_supports_reasoning("openai", "openai/gpt-6-astra") is True
    assert model_supports_reasoning("openai", "openai/gpt-4.1") is False


def test_unknown_or_blank_defaults_false():
    # Unknown/custom models default to non-reasoning so the think-tool is enabled.
    assert model_supports_reasoning("openai", "totally-made-up-model") is False
    assert model_supports_reasoning("", "gpt-6-astra") is False
    assert model_supports_reasoning("openai", "") is False


def test_env_override_marks_model_reasoning_capable(monkeypatch):
    # The escape hatch can force-mark a custom/proxy model as reasoning-capable
    # (which DISABLES the think-tool for it). Accepts ``provider/model`` and bare.
    monkeypatch.setenv("CREMIND_REASONING_MODELS", "ollama/llama3.3:70b, custom-r1")
    assert model_supports_reasoning("ollama", "llama3.3:70b") is True
    assert model_supports_reasoning("ollama", "custom-r1") is True
    assert model_supports_reasoning("ollama", "llama3.1:8b") is False
