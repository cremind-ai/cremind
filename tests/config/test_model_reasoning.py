"""Unit tests for ``model_supports_reasoning`` (the native-reasoning capability
flag that gates the ``reasoning`` think-tool).

Exercises the real provider catalogs in ``app/config/providers/*.toml`` so these
also act as a guard that the ``supports_reasoning`` flags parse and are set on
the models we expect.
"""

from __future__ import annotations

from app.config import model_supports_reasoning


def test_reasoning_model_flagged_true():
    # o-series and GPT-5.x are reasoning models; Claude 4.x supports extended thinking.
    assert model_supports_reasoning("openai", "o3") is True
    assert model_supports_reasoning("openai", "gpt-5.4") is True
    assert model_supports_reasoning("anthropic", "claude-opus-4-7") is True
    assert model_supports_reasoning("xai", "grok-3-mini") is True


def test_non_reasoning_model_flagged_false():
    # GPT-4.1 / Claude 3.5 Haiku / Gemini 2.0 are not reasoning models.
    assert model_supports_reasoning("openai", "gpt-4.1") is False
    assert model_supports_reasoning("anthropic", "claude-3-5-haiku-20241022") is False
    assert model_supports_reasoning("vertexai", "google/gemini-2.0-flash") is False


def test_provider_prefix_is_stripped():
    # The model id may arrive prefixed with ``<provider>/``.
    assert model_supports_reasoning("openai", "openai/o3") is True
    assert model_supports_reasoning("openai", "openai/gpt-4.1") is False


def test_unknown_or_blank_defaults_false():
    # Unknown/custom models default to non-reasoning so the think-tool is enabled.
    assert model_supports_reasoning("openai", "totally-made-up-model") is False
    assert model_supports_reasoning("", "o3") is False
    assert model_supports_reasoning("openai", "") is False


def test_env_override_marks_model_reasoning_capable(monkeypatch):
    # The escape hatch can force-mark a custom/proxy model as reasoning-capable
    # (which DISABLES the think-tool for it). Accepts ``provider/model`` and bare.
    monkeypatch.setenv("CREMIND_REASONING_MODELS", "ollama/llama3.3:70b, custom-r1")
    assert model_supports_reasoning("ollama", "llama3.3:70b") is True
    assert model_supports_reasoning("ollama", "custom-r1") is True
    assert model_supports_reasoning("ollama", "llama3.1:8b") is False
