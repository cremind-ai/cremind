"""Unit tests for ``model_parallel_tool_calls`` — the per-model flag controlling
whether the model may emit several tool calls in one turn.

Unlike ``supports_reasoning``/``prompt_cache``, the default is **True** (parallel
tool use is the default-on behavior for every provider). The real provider catalogs
are exercised for the default-true / prefix cases; the opt-out (``false``) branch is
exercised against a stubbed catalog since no shipped model sets it.
"""

from __future__ import annotations

import app.config as cfg
from app.config import model_parallel_tool_calls


def test_flagged_true_in_real_catalogs():
    assert model_parallel_tool_calls("anthropic", "claude-opus-4-7") is True
    assert model_parallel_tool_calls("openai", "gpt-5.4") is True


def test_unknown_or_blank_defaults_true():
    # Default is True (opposite of supports_reasoning's False default).
    assert model_parallel_tool_calls("anthropic", "totally-made-up-model") is True
    assert model_parallel_tool_calls("", "claude-opus-4-7") is True
    assert model_parallel_tool_calls("anthropic", "") is True


def test_provider_prefix_is_stripped():
    assert model_parallel_tool_calls("anthropic", "anthropic/claude-opus-4-7") is True


def test_explicit_false_opts_out(monkeypatch):
    # A model entry can opt out by setting the flag to false.
    monkeypatch.setattr(
        cfg, "load_provider_catalog",
        lambda prov: {"models": [{"id": "no-parallel", "parallel_tool_calls": False}]},
    )
    assert cfg.model_parallel_tool_calls("someprov", "no-parallel") is False


def test_listed_without_flag_defaults_true(monkeypatch):
    # A listed model that omits the flag still defaults to True.
    monkeypatch.setattr(
        cfg, "load_provider_catalog",
        lambda prov: {"models": [{"id": "unflagged"}]},
    )
    assert cfg.model_parallel_tool_calls("someprov", "unflagged") is True
