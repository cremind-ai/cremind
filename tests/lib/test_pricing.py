"""Cache-aware cost estimation: explicit per-model cache prices in the provider
catalogs must override the family-default multipliers, and the four-way cost
formula must sum the disjoint token components correctly.
"""

from __future__ import annotations

import pytest

from app.lib.llm import pricing
from app.lib.llm.pricing import compute_cost, get_model_rates


# ── explicit catalog cache prices win ───────────────────────────────────────

def test_anthropic_explicit_cache_prices_from_catalog():
    r = get_model_rates("anthropic", "claude-opus-4-7")
    assert r.source == "catalog"
    assert r.input_per_1m == 5.00
    assert r.cache_read_per_1m == 0.50    # 0.10x input
    assert r.cache_write_per_1m == 6.25   # 1.25x input


@pytest.mark.parametrize(
    "provider, model, exp_read, exp_write",
    [
        # Anthropic: read 0.10x, 5-min write 1.25x.
        ("anthropic", "claude-sonnet-4-6", 0.30, 3.75),
        # OpenAI GPT-4.1: read 0.25x, no cache-write fee (the GPT-5 family is 0.10x).
        ("openai", "gpt-4.1", 0.50, 0.0),
        # Gemini implicit caching: read 0.10x, no cache-write fee.
        ("google-gemini", "gemini-2.5-pro", 0.125, 0.0),
        # xAI publishes a FLAT cached rate ($0.20/M) — not a 0.5x-input multiple;
        # this only resolves correctly because the catalog value overrides the
        # OpenAI-family default.
        ("xai", "grok-4", 0.20, 0.0),
        # MiniMax charges a cache WRITE fee (1.25x) despite being OpenAI-family
        # (whose default write multiplier is 0.0) — proves the explicit write
        # price overrides the family default.
        ("minimax", "MiniMax-M2.7", 0.06, 0.375),
    ],
)
def test_representative_family_cache_rates(provider, model, exp_read, exp_write):
    r = get_model_rates(provider, model)
    assert r.source == "catalog"
    assert r.cache_read_per_1m == pytest.approx(exp_read)
    assert r.cache_write_per_1m == pytest.approx(exp_write)


# ── the four-way cost formula sums disjoint components ──────────────────────

def test_compute_cost_sums_four_components():
    cost = compute_cost(
        "anthropic", "claude-opus-4-7",
        input_tokens=1_000,            # 1000/1e6 * 5.00   = 0.005
        cache_read_input_tokens=2_000,  # 2000/1e6 * 0.50  = 0.001
        cache_creation_input_tokens=500,  # 500/1e6 * 6.25 = 0.003125
        output_tokens=800,             # 800/1e6 * 25.00   = 0.02
    )
    assert cost.uncached_input_usd == pytest.approx(0.005)
    assert cost.cache_read_usd == pytest.approx(0.001)
    assert cost.cache_write_usd == pytest.approx(0.003125)
    assert cost.output_usd == pytest.approx(0.02)
    assert cost.total_usd == pytest.approx(0.029125)


# ── fallback: derive from the family multiplier when no explicit fields ─────

def test_fallback_derives_from_multiplier_when_no_explicit(monkeypatch):
    """A catalog model without cache_*_price fields still resolves cache rates
    as input * family multiplier — proving the explicit prices are an override,
    not a requirement."""
    synthetic = {"models": [{
        "id": "synthetic-x",
        "input_price_per_1m": 1.00,
        "output_price_per_1m": 2.00,
    }]}
    monkeypatch.setattr(pricing, "load_provider_catalog", lambda _p: synthetic)
    get_model_rates.cache_clear()
    try:
        # OpenAI family: read 0.50x, write 0.0x.
        openai_r = get_model_rates("openai", "synthetic-x")
        assert openai_r.source == "catalog"
        assert openai_r.cache_read_per_1m == pytest.approx(0.50)
        assert openai_r.cache_write_per_1m == pytest.approx(0.0)

        # Anthropic family: read 0.10x, write 1.25x.
        anthropic_r = get_model_rates("anthropic", "synthetic-x")
        assert anthropic_r.cache_read_per_1m == pytest.approx(0.10)
        assert anthropic_r.cache_write_per_1m == pytest.approx(1.25)
    finally:
        get_model_rates.cache_clear()


def test_zero_priced_model_yields_zero_cache_rates(monkeypatch):
    synthetic = {"models": [{
        "id": "free-x",
        "input_price_per_1m": 0.0,
        "output_price_per_1m": 0.0,
    }]}
    monkeypatch.setattr(pricing, "load_provider_catalog", lambda _p: synthetic)
    get_model_rates.cache_clear()
    try:
        r = get_model_rates("litellm", "free-x")
        assert r.source == "catalog"          # 0.0 is a known price, not unknown
        assert r.cache_read_per_1m == 0.0
        assert r.cache_write_per_1m == 0.0
    finally:
        get_model_rates.cache_clear()


def test_unknown_model_yields_none_rates():
    r = get_model_rates("anthropic", "model-that-does-not-exist")
    assert r.source == "unknown"
    assert r.input_per_1m is None
    assert r.cache_read_per_1m is None
    assert r.cache_write_per_1m is None
    # cost stays null rather than silently zero
    assert compute_cost("anthropic", "model-that-does-not-exist",
                        input_tokens=1000).total_usd is None
