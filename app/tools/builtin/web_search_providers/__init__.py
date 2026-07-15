"""Web-search provider registry + order resolution.

The ``web_search`` tool selects a provider from ``WEB_SEARCH_PROVIDER``:

- ``"parallel"`` (default) -- Parallel.ai free MCP only (clean, keyless).
- ``"duckduckgo"``         -- DuckDuckGo HTML scraping only (experimental).
- ``"auto"``               -- try ``parallel`` then fall through to ``duckduckgo``.

Adding a provider later (e.g. Tavily, Anthropic-native) is: a new module that
implements the :class:`~app.tools.builtin.web_search_providers.base.SearchProvider`
protocol, one entry in :data:`_PROVIDER_FACTORIES`, and one ``_ORDERS`` value.
Factories are lazy so an unused provider's heavy imports never load.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from app.tools.builtin.web_search_providers.base import SearchProvider

DEFAULT_PROVIDER = "parallel"

# Provider id -> zero-arg factory (lazy import inside).
_PROVIDER_FACTORIES: Dict[str, Callable[[], SearchProvider]] = {}


def _make_parallel() -> SearchProvider:
    from app.tools.builtin.web_search_providers.parallel import ParallelProvider
    return ParallelProvider()


def _make_duckduckgo() -> SearchProvider:
    from app.tools.builtin.web_search_providers.duckduckgo import DuckDuckGoProvider
    return DuckDuckGoProvider()


_PROVIDER_FACTORIES["parallel"] = _make_parallel
_PROVIDER_FACTORIES["duckduckgo"] = _make_duckduckgo

# Resolved provider order per ``WEB_SEARCH_PROVIDER`` selection. Anything not
# listed falls back to the default single-provider chain.
_ORDERS: Dict[str, List[str]] = {
    "parallel": ["parallel"],
    "duckduckgo": ["duckduckgo"],
    "auto": ["parallel", "duckduckgo"],
}


def resolve_provider_order(selection: str | None) -> List[str]:
    """Return the ordered provider ids for a ``WEB_SEARCH_PROVIDER`` value."""
    key = (selection or DEFAULT_PROVIDER).strip().lower()
    return list(_ORDERS.get(key, _ORDERS[DEFAULT_PROVIDER]))


def is_explicit_selection(selection: str | None) -> bool:
    """True when the user pinned a single provider (so we should NOT fall through)."""
    key = (selection or DEFAULT_PROVIDER).strip().lower()
    return key in ("parallel", "duckduckgo")


def get_provider(provider_id: str) -> SearchProvider:
    """Instantiate a provider by id (raises ``KeyError`` if unknown)."""
    return _PROVIDER_FACTORIES[provider_id]()
