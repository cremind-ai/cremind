"""Unit tests for ``model_supports_auth_method`` and ``models_for_auth_method``.

Exercises the real provider catalogs in ``app/config/providers/*.toml`` so these
also guard that the OpenAI ``auth_methods`` flags parse and are set on the models
we expect (API-key-only vs Codex-eligible) — the distinction that gates whether a
model can run on the ChatGPT Codex backend.
"""

from __future__ import annotations

from app.config import (
    load_provider_catalog,
    model_supports_auth_method,
    models_for_auth_method,
)


def test_api_key_only_model_rejected_under_codex_oauth():
    # gpt-4.1-mini is auth_methods = ["api_key"] — the reported bug's model.
    assert model_supports_auth_method("openai", "gpt-4.1-mini", "api_key") is True
    assert model_supports_auth_method("openai", "gpt-4.1-mini", "codex_oauth") is False


def test_dual_and_codex_only_models():
    # gpt-5.4 / gpt-5.4-mini list both api_key and codex_oauth.
    assert model_supports_auth_method("openai", "gpt-5.4", "api_key") is True
    assert model_supports_auth_method("openai", "gpt-5.4", "codex_oauth") is True
    assert model_supports_auth_method("openai", "gpt-5.4-mini", "codex_oauth") is True
    # Codex-only models can't run under the API-key path.
    assert model_supports_auth_method("openai", "gpt-5.6-sol", "codex_oauth") is True
    assert model_supports_auth_method("openai", "gpt-5.6-sol", "api_key") is False
    assert model_supports_auth_method("openai", "gpt-5.3-codex-spark", "codex_oauth") is True


def test_permissive_defaults():
    # Unknown/unlisted model, blank auth method, blank provider/model → usable.
    assert model_supports_auth_method("openai", "totally-made-up", "codex_oauth") is True
    assert model_supports_auth_method("openai", "gpt-4.1-mini", None) is True
    assert model_supports_auth_method("", "gpt-4.1-mini", "codex_oauth") is True
    assert model_supports_auth_method("openai", "", "codex_oauth") is True
    # Other providers declare no per-model auth_methods → always usable.
    assert model_supports_auth_method("anthropic", "claude-opus-4-8", "api_key") is True


def test_provider_prefix_is_stripped():
    assert model_supports_auth_method("openai", "openai/gpt-4.1-mini", "codex_oauth") is False
    assert model_supports_auth_method("openai", "openai/gpt-5.4", "codex_oauth") is True


def test_models_for_auth_method_filters_real_catalog():
    catalog = load_provider_catalog("openai")
    codex_ids = {m["id"] for m in models_for_auth_method(catalog, "codex_oauth")}
    apikey_ids = {m["id"] for m in models_for_auth_method(catalog, "api_key")}
    # Codex set includes the codex-only + dual models, excludes api_key-only ones.
    assert "gpt-5.6-sol" in codex_ids
    assert "gpt-5.4" in codex_ids
    assert "gpt-4.1-mini" not in codex_ids
    # API-key set includes the api_key-only + dual models, excludes codex-only ones.
    assert "gpt-4.1-mini" in apikey_ids
    assert "gpt-5.4" in apikey_ids
    assert "gpt-5.6-sol" not in apikey_ids


def test_models_for_auth_method_no_filter_when_auth_none():
    catalog = load_provider_catalog("openai")
    assert len(models_for_auth_method(catalog, None)) == len(catalog.get("models", []))
