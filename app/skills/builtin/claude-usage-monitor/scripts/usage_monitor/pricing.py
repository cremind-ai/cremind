"""API list prices, used to weigh Claude Code's per-reply token counts into one figure.

Subscription limits aren't published in tokens, but list-price-weighted usage tracks
them closely: on 2026-10-06 two Max 20x accounts measured $590 and $550 per full
5-hour window. The monitor keeps that ratio calibrated against official readings.
"""

from __future__ import annotations

import math
import re

# $ per million tokens: (input, output, cache write 5 min, cache write 1 h, cache read)
PRICES: list[tuple[re.Pattern[str], tuple[float, float, float, float, float]]] = [
    (re.compile(r"fable-5-1|mythos-5-1"), (10, 50, 12.5, 20, 0.25)),
    (re.compile(r"fable|mythos"), (10, 50, 12.5, 20, 1)),
    (re.compile(r"opus-5-5"), (4, 20, 5, 8, 0.2)),
    (re.compile(r"opus-4-1|opus-4-2025|opus-4-0"), (15, 75, 18.75, 30, 1.5)),
    (re.compile(r"opus"), (5, 25, 6.25, 10, 0.5)),
    (re.compile(r"sonnet-5"), (2, 10, 2.5, 4, 0.2)),
    (re.compile(r"sonnet"), (3, 15, 3.75, 6, 0.3)),
    (re.compile(r"haiku"), (1, 5, 1.25, 2, 0.1)),
]


def prices_for(model: object) -> tuple[float, float, float, float, float] | None:
    model_id = str(model or "").lower()
    for pattern, prices in PRICES:
        if pattern.search(model_id):
            return prices
    return None


def _n(value: object) -> float:
    """A positive, finite token count, else 0 (bools are not counts)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def reply_cost(model: object, usage: object) -> float | None:
    """List-price USD of one API reply, or None for a model without a known price."""
    prices = prices_for(model)
    if not prices or not isinstance(usage, dict):
        return None
    cache_creation = usage.get("cache_creation")
    cc = cache_creation if isinstance(cache_creation, dict) else {}
    write_1h = _n(cc.get("ephemeral_1h_input_tokens"))
    if cc.get("ephemeral_5m_input_tokens") is not None:
        write_5m = _n(cc.get("ephemeral_5m_input_tokens"))
    else:
        write_5m = max(0.0, _n(usage.get("cache_creation_input_tokens")) - write_1h)
    usd = (
        _n(usage.get("input_tokens")) * prices[0]
        + _n(usage.get("output_tokens")) * prices[1]
        + write_5m * prices[2]
        + write_1h * prices[3]
        + _n(usage.get("cache_read_input_tokens")) * prices[4]
    ) / 1e6
    return usd * 2 if usage.get("speed") == "fast" else usd
