"""Tests for ``ModelGroupManager`` group resolution.

The ``low`` group was un-collapsed so it is a real, separately-configurable
model (used by the skill-event matching gate and any future cheap-model
feature). It resolves its own ``model_group.low`` key and falls back to the
single configured model (``high``) when unset — mirroring ``vision``. ``main``
remains an alias for ``high``.
"""

from __future__ import annotations

from app.lib.llm.model_groups import ModelGroupManager


class _FakeConfig:
    def __init__(self, values=None):
        self._v = values or {}

    def get(self, table, key, profile=None):  # noqa: D401 - mimics DynamicConfigStorage.get
        return self._v.get(key)


def test_low_resolves_its_own_key_independently():
    mgr = ModelGroupManager(_FakeConfig({
        "model_group.high": "anthropic/claude-opus-4-8",
        "model_group.low": "groq/llama-3.1-8b-instant",
    }))
    assert mgr.get_provider_and_model("low") == ("groq", "llama-3.1-8b-instant")
    # high is unaffected by the low override.
    assert mgr.get_provider_and_model("high") == ("anthropic", "claude-opus-4-8")


def test_low_falls_back_to_high_when_unset():
    mgr = ModelGroupManager(_FakeConfig({
        "model_group.high": "anthropic/claude-opus-4-8",
    }))
    # No model_group.low and no TOML default → transparently uses the main model.
    assert mgr.get_provider_and_model("low") == ("anthropic", "claude-opus-4-8")


def test_main_is_alias_for_high():
    mgr = ModelGroupManager(_FakeConfig({"model_group.high": "groq/foo/bar"}))
    assert mgr.get_provider_and_model("main") == mgr.get_provider_and_model("high")


def _capture_provider_and_model(monkeypatch):
    """Patch ``create_llm_provider`` to record the (provider, model) it's handed.

    ``create_llm_for_tool`` builds a real provider; we only care which group it
    resolved, so we stub provider construction and capture its first two args.
    """
    captured = {}

    def _fake_create(provider_name, model_name, **kwargs):
        captured["provider"] = provider_name
        captured["model"] = model_name
        return object()  # stand-in LLMProvider; identity is irrelevant here

    monkeypatch.setattr(
        "app.lib.llm.model_groups.create_llm_provider", _fake_create
    )
    return captured


def test_documentation_search_resolves_low_model(monkeypatch):
    captured = _capture_provider_and_model(monkeypatch)
    mgr = ModelGroupManager(_FakeConfig({
        "model_group.high": "anthropic/claude-opus-4-8",
        "model_group.low": "groq/llama-3.1-8b-instant",
    }))
    mgr.create_llm_for_tool("documentation_search")
    assert (captured["provider"], captured["model"]) == ("groq", "llama-3.1-8b-instant")


def test_generic_tool_resolves_high_model(monkeypatch):
    captured = _capture_provider_and_model(monkeypatch)
    mgr = ModelGroupManager(_FakeConfig({
        "model_group.high": "anthropic/claude-opus-4-8",
        "model_group.low": "groq/llama-3.1-8b-instant",
    }))
    mgr.create_llm_for_tool("web_search")
    assert (captured["provider"], captured["model"]) == ("anthropic", "claude-opus-4-8")


def test_documentation_search_falls_back_to_high_when_low_unset(monkeypatch):
    captured = _capture_provider_and_model(monkeypatch)
    mgr = ModelGroupManager(_FakeConfig({
        "model_group.high": "anthropic/claude-opus-4-8",
    }))
    # No model_group.low configured → judge transparently uses the main model.
    mgr.create_llm_for_tool("documentation_search")
    assert (captured["provider"], captured["model"]) == ("anthropic", "claude-opus-4-8")
