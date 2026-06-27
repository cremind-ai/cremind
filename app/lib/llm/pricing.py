"""Cache-aware cost estimation for LLM token usage.

Maps a ``(provider, model)`` pair to per-1M-token rates from the provider
catalogs (``app/config/providers/*.toml``) and computes an estimated USD cost
from Cremind's four-way token breakdown (uncached input, cache-read,
cache-write, output).

Design notes
------------
* Cost is computed at *write* time and frozen on the ``usage_records`` row, so a
  historical estimate never moves when catalog prices change. Raw token counts
  are always kept alongside, so a record can be re-priced on demand
  (:func:`recompute_cost_from_record`).
* Cache pricing differs by provider family:
    - **Anthropic** keeps cache tokens distinct from input. Cache *reads* cost
      ~0.10x the input rate; 5-minute ephemeral cache *writes* cost ~1.25x the
      input rate (per Anthropic's published prompt-caching economics).
    - **OpenAI-family** runtimes fold cached tokens into the prompt and never
      charge separately for cache creation — Cremind's
      ``openai_usage_breakdown()`` always reports ``cache_creation`` as 0, so the
      write multiplier is moot for those providers. The actual cached-read
      discount varies by model (e.g. the GPT-5 family is 0.10x, GPT-4.1 is 0.25x),
      so models carry explicit ``cache_read_price_per_1m`` fields; the 0.50x
      family default below is only a conservative fallback for entries that omit
      one — it over-estimates rather than treats cached tokens as free.
* A model entry may carry optional ``cache_read_price_per_1m`` /
  ``cache_write_price_per_1m`` fields; when present they override the
  family-default multipliers. Absent those, rates are derived as
  ``input_price_per_1m * multiplier``.

This module must not import from ``app.storage`` — storage imports pricing, not
the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from app.config import load_provider_catalog

# Bump when the multiplier table or cost logic changes, so a stored
# ``rate_snapshot`` records which generation of the estimator produced it.
PRICING_VERSION = 1

# provider-family -> (cache_read_multiplier, cache_write_multiplier) relative to
# the input rate, used when a model has no explicit cache_*_price_per_1m fields.
#   anthropic     : read 0.10x, 5-min write 1.25x (cache tokens are distinct)
#   openai-family : read 0.50x, write 0 — conservative fallback only; real OpenAI
#                   models set explicit cache_read_price_per_1m (GPT-5 0.10x,
#                   GPT-4.1 0.25x). Creation tokens are always 0 here.
#   google/gemini : read 0.10x, write 0 (implicit context-cache discount)
#   fallback      : 1.0 / 1.0 — unknown cache economics over-estimate rather
#                   than silently treat cached tokens as free.
_CACHE_MULTIPLIERS: dict[str, tuple[float, float]] = {
    "anthropic": (0.10, 1.25),
    "openai": (0.50, 0.0),
    "google": (0.10, 0.0),
    "fallback": (1.0, 1.0),
}

# Map a provider name (the catalog stem / LLMProvider.provider_name) to a family
# key in ``_CACHE_MULTIPLIERS``. Anything not listed falls back to the
# OpenAI-compatible family, since every non-Anthropic provider in Cremind routes
# usage through ``openai_usage_breakdown()`` (cache_creation == 0).
_FAMILY_BY_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "google-gemini": "google",
    "gemini": "google",
    "google": "google",
}


@dataclass(frozen=True)
class ModelRates:
    """Resolved per-1M-token rates for a single model.

    ``input_per_1m`` / ``output_per_1m`` are ``None`` for an unknown / unlisted
    model — in that case cost cannot be estimated and stays ``None`` (never
    silently zero). ``cache_*_per_1m`` are the resolved cache rates (explicit
    catalog value, or ``input_per_1m * multiplier``).
    """

    provider: Optional[str]
    model: Optional[str]
    model_group: Optional[str]
    input_per_1m: Optional[float]
    output_per_1m: Optional[float]
    cache_read_per_1m: Optional[float]
    cache_write_per_1m: Optional[float]
    cache_read_multiplier: float
    cache_write_multiplier: float
    source: str  # "catalog" | "unknown"


@dataclass(frozen=True)
class CostBreakdown:
    """Per-component USD cost plus the rate snapshot used to produce it."""

    uncached_input_usd: Optional[float]
    cache_read_usd: Optional[float]
    cache_write_usd: Optional[float]
    output_usd: Optional[float]
    total_usd: Optional[float]
    rate_snapshot: dict


def provider_family(provider: Optional[str]) -> str:
    """Normalize a provider name to a cache-multiplier family key."""
    if not provider:
        return "fallback"
    return _FAMILY_BY_PROVIDER.get(provider.lower(), "openai")


def _strip_provider_prefix(provider: Optional[str], model: str) -> str:
    """Drop a leading ``provider/`` from a model id (mirrors config helpers)."""
    if provider:
        prefix = f"{provider}/"
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


@lru_cache(maxsize=512)
def get_model_rates(provider: Optional[str], model: Optional[str]) -> ModelRates:
    """Resolve per-1M rates for ``(provider, model)`` from the TOML catalog.

    Unknown provider/model (or a listed model with no ``input_price_per_1m``)
    yields ``input_per_1m`` / ``output_per_1m`` of ``None`` and ``source =
    "unknown"`` so the caller leaves cost null. ``lru_cache``-d on the pair.
    """
    family = provider_family(provider)
    read_mult, write_mult = _CACHE_MULTIPLIERS[family]

    if not provider or not model:
        return ModelRates(
            provider=provider, model=model, model_group=None,
            input_per_1m=None, output_per_1m=None,
            cache_read_per_1m=None, cache_write_per_1m=None,
            cache_read_multiplier=read_mult, cache_write_multiplier=write_mult,
            source="unknown",
        )

    bare = _strip_provider_prefix(provider, model)
    catalog = load_provider_catalog(provider)
    entry: dict[str, Any] = {}
    for candidate in catalog.get("models", []) or []:
        if isinstance(candidate, dict) and candidate.get("id") == bare:
            entry = candidate
            break

    input_per_1m = _as_float(entry.get("input_price_per_1m"))
    output_per_1m = _as_float(entry.get("output_price_per_1m"))
    model_group = entry.get("group_hint")

    # Explicit cache rates win; otherwise derive from the input rate.
    explicit_read = _as_float(entry.get("cache_read_price_per_1m"))
    explicit_write = _as_float(entry.get("cache_write_price_per_1m"))
    cache_read_per_1m = (
        explicit_read if explicit_read is not None
        else (input_per_1m * read_mult if input_per_1m is not None else None)
    )
    cache_write_per_1m = (
        explicit_write if explicit_write is not None
        else (input_per_1m * write_mult if input_per_1m is not None else None)
    )

    source = "catalog" if input_per_1m is not None else "unknown"
    return ModelRates(
        provider=provider, model=model, model_group=model_group,
        input_per_1m=input_per_1m, output_per_1m=output_per_1m,
        cache_read_per_1m=cache_read_per_1m, cache_write_per_1m=cache_write_per_1m,
        cache_read_multiplier=read_mult, cache_write_multiplier=write_mult,
        source=source,
    )


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _component(tokens: Optional[int], rate_per_1m: Optional[float]) -> Optional[float]:
    if rate_per_1m is None:
        return None
    return (int(tokens or 0) / 1_000_000) * rate_per_1m


def compute_cost(
    provider: Optional[str],
    model: Optional[str],
    *,
    input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    output_tokens: int = 0,
    rates: Optional[ModelRates] = None,
) -> CostBreakdown:
    """Estimate USD cost from a four-way token breakdown.

    Each component is ``tokens / 1e6 * rate``; a component whose rate is unknown
    is ``None``. ``total_usd`` is the sum of the known components (``None`` only
    when every component is unknown). ``rate_snapshot`` always captures the
    resolved rates, multipliers, ``source`` and ``PRICING_VERSION``.
    """
    rates = rates or get_model_rates(provider, model)

    uncached_input_usd = _component(input_tokens, rates.input_per_1m)
    cache_read_usd = _component(cache_read_input_tokens, rates.cache_read_per_1m)
    cache_write_usd = _component(cache_creation_input_tokens, rates.cache_write_per_1m)
    output_usd = _component(output_tokens, rates.output_per_1m)

    components = [uncached_input_usd, cache_read_usd, cache_write_usd, output_usd]
    known = [c for c in components if c is not None]
    total_usd = sum(known) if known else None

    rate_snapshot = {
        "input_per_1m": rates.input_per_1m,
        "output_per_1m": rates.output_per_1m,
        "cache_read_per_1m": rates.cache_read_per_1m,
        "cache_write_per_1m": rates.cache_write_per_1m,
        "cache_read_multiplier": rates.cache_read_multiplier,
        "cache_write_multiplier": rates.cache_write_multiplier,
        "source": rates.source,
        "pricing_version": PRICING_VERSION,
    }
    return CostBreakdown(
        uncached_input_usd=uncached_input_usd,
        cache_read_usd=cache_read_usd,
        cache_write_usd=cache_write_usd,
        output_usd=output_usd,
        total_usd=total_usd,
        rate_snapshot=rate_snapshot,
    )


def cost_columns_for(
    provider: Optional[str],
    model: Optional[str],
    token_usage: Optional[dict],
) -> dict:
    """Return cost columns ready to splat into a ``UsageRecordModel`` row.

    ``token_usage`` uses Cremind's canonical four keys (``input_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``,
    ``output_tokens``). Always succeeds — an unknown model yields null costs.
    """
    usage = token_usage or {}
    rates = get_model_rates(provider, model)
    cost = compute_cost(
        provider, model,
        input_tokens=int(usage.get("input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        rates=rates,
    )
    return {
        "uncached_input_usd": cost.uncached_input_usd,
        "cache_read_usd": cost.cache_read_usd,
        "cache_write_usd": cost.cache_write_usd,
        "output_usd": cost.output_usd,
        "total_usd": cost.total_usd,
        "rate_snapshot": cost.rate_snapshot,
        "model_group": rates.model_group,
    }


def recompute_cost_from_record(record: Any) -> CostBreakdown:
    """Re-price a stored record from its raw tokens at *current* catalog rates.

    For an admin "estimate at today's prices" view only — never the normal read
    path, which reads the frozen ``*_usd`` columns. ``record`` may be an ORM row
    or a mapping exposing the four token fields plus ``provider`` / ``model``.
    """
    get = record.get if isinstance(record, dict) else lambda k: getattr(record, k, None)
    return compute_cost(
        get("provider"), get("model"),
        input_tokens=int(get("input_tokens") or 0),
        cache_read_input_tokens=int(get("cache_read_input_tokens") or 0),
        cache_creation_input_tokens=int(get("cache_creation_input_tokens") or 0),
        output_tokens=int(get("output_tokens") or 0),
    )
