"""Web Search built-in tool (pluggable, zero-config).

One ``search_web`` tool backed by a pluggable provider registry. The default
provider is Parallel.ai's free, keyless MCP endpoint; DuckDuckGo HTML scraping
is available as an opt-in, experimental fallback. See
``app.tools.builtin.web_search_providers`` for the providers and
``WEB_SEARCH_PROVIDER`` selection.

Invocation
----------
The reasoning model calls ``search_web`` directly via native function
calling, filling the ``query`` argument from the tool's JSON-Schema. There
is no per-group routing LLM. ``get_tools`` exposes exactly one sub-tool.

Zero-config
-----------
Works with no API key. ``required_config`` exposes only OPTIONAL knobs (provider
choice, DDG region/safe-search, an optional Parallel API key) with defaults;
``run()`` self-defaults from ``_variables`` because the adapter passes only
DB-persisted variables (it does not auto-merge ``required_config`` defaults).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.external_content import wrap_web_content
from app.tools.builtin.web_search_providers import (
    get_provider,
    is_explicit_selection,
    resolve_provider_order,
)
from app.tools.builtin.web_search_providers.base import ProviderError
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Web Search"

DEFAULT_COUNT = 5
MAX_COUNT = 10
CACHE_TTL_SECONDS = 15 * 60  # 15 minutes


class Var:
    """Optional config keys for the Web Search tool (all have defaults)."""
    PROVIDER = "WEB_SEARCH_PROVIDER"   # "parallel" | "duckduckgo" | "auto"
    # DDG-only knobs + an optional Parallel key are declared so they surface in
    # the Settings UI; the providers read them from ``_variables``.
    DDG_REGION = "DDG_REGION"
    DDG_SAFE_SEARCH = "DDG_SAFE_SEARCH"
    PARALLEL_API_KEY = "PARALLEL_API_KEY"


TOOL_CONFIG: ToolConfig = {
    "name": "web_search",
    "display_name": SERVER_NAME,
    "description": (
        "Searches the public web and returns ranked results with snippets. "
        "Use it as the last-resort internet fallback when local tools "
        "(documentation and memory) cannot answer, or when the user "
        "explicitly wants fresh external information."
    ),
    "visible": True,
    "required_config": {
        Var.PROVIDER: {
            "description": (
                "Search backend. 'parallel' (default) uses Parallel.ai's free, "
                "keyless API. 'duckduckgo' scrapes DuckDuckGo's HTML endpoint "
                "(experimental/unofficial, rate-limited — use at your own "
                "risk). 'auto' tries parallel then falls back to duckduckgo."
            ),
            "type": "string",
            "enum": ["parallel", "duckduckgo", "auto"],
            "default": "parallel",
        },
        Var.PARALLEL_API_KEY: {
            "description": (
                "Optional Parallel.ai API key. Provide it to use the "
                "higher-rate-limit authenticated endpoint instead of the free "
                "anonymous one. Leave blank to use the free tier."
            ),
            "type": "string",
            "secret": True,
            "default": "",
        },
        Var.DDG_REGION: {
            "description": (
                "Optional DuckDuckGo region code (e.g. 'us-en', 'uk-en', "
                "'de-de'). Only used by the 'duckduckgo' provider."
            ),
            "type": "string",
            "default": "",
        },
        Var.DDG_SAFE_SEARCH: {
            "description": "Safe-search level for the 'duckduckgo' provider.",
            "type": "string",
            "enum": ["strict", "moderate", "off"],
            "default": "moderate",
        },
    },
}


# ---------------------------------------------------------------------------
# In-memory TTL cache (process-local; keyed by provider/query/count/region/safe)
# ---------------------------------------------------------------------------

# key -> (expires_at_monotonic, payload)
_SEARCH_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _cache_key(selection: str, query: str, count: int, region: str, safe: str) -> str:
    return "\x1f".join([selection, query.strip().lower(), str(count), region, safe])


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _SEARCH_CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if time.monotonic() >= expires_at:
        _SEARCH_CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: str, payload: Dict[str, Any]) -> None:
    _SEARCH_CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, payload)


class WebSearchTool(BuiltInTool):
    name: str = "search_web"
    description: str = (
        "Search the public web and return a ranked list of results, each with "
        "a title, URL and snippet. Use the Web Fetch tool to read the full "
        "content of a result."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "count": {
                "type": "integer",
                "description": (
                    f"Number of results to return (default {DEFAULT_COUNT}, "
                    f"max {MAX_COUNT})."
                ),
                "minimum": 1,
                "maximum": MAX_COUNT,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "A non-empty 'query' is required.",
            })

        try:
            count = int(arguments.get("count") or DEFAULT_COUNT)
        except (TypeError, ValueError):
            count = DEFAULT_COUNT
        count = max(1, min(count, MAX_COUNT))

        variables = arguments.get("_variables") or {}
        selection = (variables.get(Var.PROVIDER) or "parallel").strip().lower()
        region = (variables.get(Var.DDG_REGION) or "").strip()
        safe = (variables.get(Var.DDG_SAFE_SEARCH) or "moderate").strip().lower()

        cache_key = _cache_key(selection, query, count, region, safe)
        cached = _cache_get(cache_key)
        if cached is not None:
            return BuiltInToolResult(structured_content={**cached, "cached": True})

        order = resolve_provider_order(selection)
        allow_fallthrough = not is_explicit_selection(selection)

        last_error: Optional[ProviderError] = None
        for provider_id in order:
            try:
                provider = get_provider(provider_id)
            except KeyError:
                continue
            try:
                results = await provider.search(query, count=count, variables=variables)
            except ProviderError as exc:
                last_error = exc
                logger.warning(f"[web_search] provider '{provider_id}' failed: {exc.code}: {exc}")
                if allow_fallthrough:
                    continue
                return BuiltInToolResult(structured_content={
                    "error": exc.code,
                    "provider": provider_id,
                    "message": str(exc),
                })
            except Exception as exc:  # noqa: BLE001 - never leak a raw traceback to the agent
                last_error = ProviderError(str(exc))
                logger.exception(f"[web_search] provider '{provider_id}' crashed")
                if allow_fallthrough:
                    continue
                return BuiltInToolResult(structured_content={
                    "error": "ProviderError",
                    "provider": provider_id,
                    "message": str(exc),
                })

            payload = {
                "query": query,
                "provider": provider_id,
                "count": len(results),
                "external_content": {
                    "untrusted": True,
                    "source": "web_search",
                    "wrapped": True,
                },
                "results": [
                    {
                        "title": wrap_web_content(r.get("title", ""), source="web_search"),
                        "url": r.get("url", ""),
                        "snippet": (
                            wrap_web_content(r["snippet"], source="web_search")
                            if r.get("snippet") else ""
                        ),
                    }
                    for r in results
                ],
            }
            _cache_set(cache_key, payload)
            return BuiltInToolResult(structured_content=payload)

        # Every provider in the chain failed.
        msg = str(last_error) if last_error else "No web-search provider available."
        return BuiltInToolResult(structured_content={
            "error": getattr(last_error, "code", "NoProvider"),
            "message": f"Web search failed: {msg}",
        })


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server.

    Exactly one tool -- the reasoning model fills its ``query`` argument
    directly via native function calling.
    """
    return [WebSearchTool()]
