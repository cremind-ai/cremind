"""Unit tests for custom-provider catalog resolution in ``app.config``.

Custom providers (``custom:<slug>``) have no TOML file — their catalog is
synthesized per-profile from the ``custom_providers`` registry row in
``llm_config``. These tests pin that ``resolve_catalog`` reads the registry and
that ``model_supports_vision`` / ``model_supports_reasoning`` honor the user's
stored per-model flags when a ``profile`` is threaded through (and fall back to
the safe ``False`` default without one, or when the slug is missing).
"""

from __future__ import annotations

import json

import pytest

from app.config import (
    model_supports_reasoning,
    model_supports_vision,
    resolve_catalog,
)

_REGISTRY = {
    "acme": {
        "display_name": "Acme",
        "base_url": "https://api.acme.ai/v1",
        "models": [
            {"id": "acme-vision", "display_name": "Acme Vision", "group_hint": "high",
             "vision": True, "supports_reasoning": False, "reasoning_effort": [],
             "input_price_per_1m": 1.0, "output_price_per_1m": 4.0,
             "cache_read_price_per_1m": 0.25, "cache_write_price_per_1m": 2.0},
            {"id": "acme-reasoner", "display_name": "Acme Reasoner", "group_hint": "high",
             "vision": False, "supports_reasoning": True, "reasoning_effort": ["low", "high"]},
        ],
    }
}


class _Store:
    """Stand-in for the dynamic-config singleton used by ``get_dynamic``."""

    def __init__(self, rows: dict[tuple[str, str, str], str]):
        self.rows = rows

    def get(self, table: str, key: str, profile: str = "admin"):
        return self.rows.get((table, key, profile))


@pytest.fixture()
def _dyn(monkeypatch: pytest.MonkeyPatch):
    store = _Store({("llm_config", "custom_providers", "admin"): json.dumps(_REGISTRY)})
    monkeypatch.setattr("app.config.settings._dynamic_config_storage", store)
    return store


def test_resolve_catalog_synthesizes_custom(_dyn) -> None:
    cat = resolve_catalog("custom:acme", profile="admin")
    assert cat["provider"]["base_url"] == "https://api.acme.ai/v1"
    assert cat["provider"]["display_name"] == "Acme"
    assert {m["id"] for m in cat["models"]} == {"acme-vision", "acme-reasoner"}


def test_resolve_catalog_missing_slug_or_profile(_dyn) -> None:
    # Deleted / unknown slug → empty catalog.
    assert resolve_catalog("custom:ghost", profile="admin") == {}
    # No profile → registry unreachable → empty (profile is required for custom).
    assert resolve_catalog("custom:acme") == {}


def test_vision_flag_honored_for_custom(_dyn) -> None:
    assert model_supports_vision("custom:acme", "acme-vision", profile="admin") is True
    assert model_supports_vision("custom:acme", "acme-reasoner", profile="admin") is False
    # Also works when the model id carries the provider prefix (UI value shape).
    assert model_supports_vision("custom:acme", "custom:acme/acme-vision", profile="admin") is True


def test_reasoning_flag_honored_for_custom(_dyn) -> None:
    assert model_supports_reasoning("custom:acme", "acme-reasoner", profile="admin") is True
    assert model_supports_reasoning("custom:acme", "acme-vision", profile="admin") is False


def test_custom_defaults_false_without_profile(_dyn) -> None:
    # Without a profile the registry is unreachable, so both capability checks
    # fall back to the safe default (False) rather than crashing.
    assert model_supports_vision("custom:acme", "acme-vision") is False
    assert model_supports_reasoning("custom:acme", "acme-reasoner") is False


def test_builtin_resolution_unaffected() -> None:
    # A non-custom name still resolves via the TOML catalog loader.
    cat = resolve_catalog("openai")
    assert cat.get("provider", {}).get("name") == "openai"
    assert isinstance(cat.get("models"), list) and cat["models"]


def test_custom_pricing_computes_cost(_dyn) -> None:
    """A custom model's user-entered prices feed the cost path (per-profile,
    bypassing the built-in lru_cache), while a price-less model stays unknown."""
    from app.lib.llm.pricing import cost_columns_for, get_model_rates

    rates = get_model_rates("custom:acme", "acme-vision", profile="admin")
    assert rates.input_per_1m == 1.0
    assert rates.output_per_1m == 4.0
    # Explicit cache rates from the model entry are used verbatim.
    assert rates.cache_read_per_1m == 0.25
    assert rates.cache_write_per_1m == 2.0
    assert rates.source == "catalog"

    cost = cost_columns_for(
        "custom:acme", "custom:acme/acme-vision",
        {"input_tokens": 1_000_000, "output_tokens": 500_000,
         "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000},
        profile="admin",
    )
    # input 1.0 + output 4.0*0.5 + cache_read 0.25 + cache_write 2.0 = 5.25
    assert cost["total_usd"] == pytest.approx(5.25)

    # A model with no price → unknown cost (null), not a silent zero.
    priceless = cost_columns_for(
        "custom:acme", "acme-reasoner", {"input_tokens": 1_000_000}, profile="admin"
    )
    assert priceless["total_usd"] is None

    # Without a profile the custom registry is unreachable → unknown, and the
    # built-in cached path is never polluted with custom entries.
    assert get_model_rates("custom:acme", "acme-vision").source == "unknown"
