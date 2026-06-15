"""Parallel.ai free MCP web-search provider (zero-config, keyless default).

Calls Parallel's streamable-HTTP MCP endpoint ``https://search.parallel.ai/mcp``
via Cremind's existing MCP client stack
(:class:`app.tools.mcp.mcp_connection.MCPConnection`, which wraps the official
``mcp`` SDK). The free endpoint requires no account or API key; passing a
Parallel API key as a Bearer token unlocks the higher-limit ``mcp-oauth``
endpoint (supported here via the optional ``PARALLEL_API_KEY`` config var).

Why this is the default
-----------------------
Unlike DuckDuckGo HTML scraping, this is a *documented, sanctioned* free API
that returns structured JSON (no fragile HTML parsing). It is what OpenClaw
prefers as its top keyless provider. Risks (no SLA, undocumented anonymous
rate limits, "light use" positioning) are handled by failing gracefully so the
tool can fall through (under ``auto``) or surface a clear error.

Robustness
----------
Parallel's docs name the tool (``web_search``) but do not pin its input/output
schema, so this provider is *schema-aware at runtime*: it reads the connected
tool's ``inputSchema`` and maps our query into whatever parameter the server
actually exposes, and parses the result defensively across several plausible
shapes.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.tools.builtin.web_search_providers.base import (
    ProviderError,
    SearchResult,
)
from app.utils.logger import logger

PROVIDER_ID = "parallel"

PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
PARALLEL_MCP_OAUTH_URL = "https://search.parallel.ai/mcp-oauth"
PARALLEL_TOOL_NAME = "web_search"
SEARCH_TIMEOUT_SECONDS = 25.0

# Optional config: a Parallel API key promotes the request to the
# higher-limit authenticated endpoint.
VAR_API_KEY = "PARALLEL_API_KEY"

# Candidate input-parameter names, in priority order, for mapping our query.
# Parallel's Search API is "objective + keywords"; the MCP tool may expose any
# of these. We set the first present scalar param to the query, and the first
# present array param to ``[query]``.
_QUERY_SCALAR_PARAMS = ("objective", "query", "q", "search_query", "prompt")
_QUERY_ARRAY_PARAMS = ("search_queries", "keywords", "queries")
_COUNT_PARAMS = ("max_results", "count", "num_results", "max_chars_per_result")


def _build_args(input_schema: Optional[Dict[str, Any]], query: str, count: int) -> Dict[str, Any]:
    """Build tool arguments matching the server's advertised ``inputSchema``.

    Falls back to a permissive ``{"objective": query}`` when no schema (or no
    recognised property) is available.
    """
    props: Dict[str, Any] = {}
    if isinstance(input_schema, dict):
        raw = input_schema.get("properties")
        if isinstance(raw, dict):
            props = raw

    args: Dict[str, Any] = {}
    if props:
        for name in _QUERY_SCALAR_PARAMS:
            if name in props:
                args[name] = query
                break
        for name in _QUERY_ARRAY_PARAMS:
            if name in props:
                args[name] = [query]
                break
        for name in _COUNT_PARAMS:
            if name in props and name != "max_chars_per_result":
                args[name] = count
                break
    if not args:
        # Unknown schema: send the most likely shape.
        args = {"objective": query}
    return args


def _coerce_results(obj: Any) -> List[Dict[str, Any]]:
    """Pull a list of result dicts out of an arbitrary structured payload."""
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for key in ("results", "data", "items", "search_results", "hits"):
            val = obj.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
    return []


def _map_item(item: Dict[str, Any]) -> Optional[SearchResult]:
    """Map one Parallel result dict to a :data:`SearchResult` (or ``None``)."""
    url = item.get("url") or item.get("link") or item.get("source_url") or ""
    if not url or not isinstance(url, str):
        return None
    title = item.get("title") or item.get("name") or item.get("heading") or ""

    snippet: str = ""
    excerpts = item.get("excerpts") or item.get("snippets")
    if isinstance(excerpts, list):
        snippet = " ".join(str(e) for e in excerpts if e)
    elif isinstance(excerpts, str):
        snippet = excerpts
    if not snippet:
        snippet = (
            item.get("snippet")
            or item.get("content")
            or item.get("text")
            or item.get("description")
            or ""
        )
    return {"title": str(title), "url": url, "snippet": str(snippet)}


def _parse_call_result(result: Any) -> List[SearchResult]:
    """Parse an MCP ``CallToolResult`` into ``SearchResult`` list.

    Prefers ``structuredContent``; otherwise concatenates text content blocks
    and tries to JSON-decode them.
    """
    if getattr(result, "isError", False):
        text = _gather_text(result)
        raise ProviderError(
            f"Parallel returned a tool error: {text[:300] or 'unknown error'}",
            code="Upstream",
        )

    raw_results: List[Dict[str, Any]] = []

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        raw_results = _coerce_results(structured)

    if not raw_results:
        text = _gather_text(result)
        if text:
            try:
                raw_results = _coerce_results(json.loads(text))
            except (json.JSONDecodeError, ValueError):
                raw_results = []

    mapped: List[SearchResult] = []
    for item in raw_results:
        sr = _map_item(item)
        if sr:
            mapped.append(sr)
    return mapped


def _gather_text(result: Any) -> str:
    """Concatenate the ``.text`` of any text content blocks on a CallToolResult."""
    parts: List[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


class ParallelProvider:
    """Keyless (or Bearer-authenticated) Parallel.ai MCP search provider."""

    id = PROVIDER_ID

    async def search(
        self, query: str, *, count: int, variables: Dict[str, Any],
    ) -> List[SearchResult]:
        # Lazy import so this module (and web_search) stays importable even if
        # the ``mcp`` SDK is somehow absent; the smoke test then skips us.
        try:
            from app.tools.mcp.mcp_connection import MCPConnection
        except ImportError as exc:  # pragma: no cover - mcp ships transitively
            raise ProviderError(
                f"MCP client unavailable for the Parallel provider: {exc}",
                code="Unavailable",
            ) from exc

        api_key = (variables.get(VAR_API_KEY) or "").strip()
        if api_key:
            url = PARALLEL_MCP_OAUTH_URL
            headers: Optional[Dict[str, str]] = {"Authorization": f"Bearer {api_key}"}
        else:
            url = PARALLEL_MCP_URL
            headers = None

        conn = MCPConnection()
        try:
            try:
                await conn.connect_http(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - normalise transport errors
                msg = str(exc).lower()
                if "401" in msg or "403" in msg or "unauthorized" in msg:
                    raise ProviderError(
                        "Parallel rejected the request (auth required).",
                        code="AuthRequired",
                    ) from exc
                if "429" in msg or "rate" in msg:
                    raise ProviderError(
                        "Parallel rate-limited the request.", code="RateLimited",
                    ) from exc
                raise ProviderError(
                    f"Could not reach Parallel MCP endpoint: {exc}", code="Network",
                ) from exc

            tool_name, input_schema = _resolve_tool(conn.get_tools())
            args = _build_args(input_schema, query, count)

            try:
                result = await conn.call_tool(tool_name, args, timeout=SEARCH_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(
                    f"Parallel search call failed: {exc}", code="Upstream",
                ) from exc

            results = _parse_call_result(result)[:count]
            logger.info(f"[web_search:parallel] '{query[:60]}' -> {len(results)} results")
            return results
        finally:
            await conn.cleanup()


def _resolve_tool(tools: List[Any]) -> tuple[str, Optional[Dict[str, Any]]]:
    """Find the search tool among the server's tools; return (name, inputSchema).

    Prefers the documented ``web_search`` name; otherwise the first tool whose
    name contains ``search``; otherwise the first tool.
    """
    if not tools:
        raise ProviderError("Parallel MCP server exposed no tools.", code="Upstream")

    def schema_of(t: Any) -> Optional[Dict[str, Any]]:
        s = getattr(t, "inputSchema", None)
        return s if isinstance(s, dict) else None

    for t in tools:
        if getattr(t, "name", "") == PARALLEL_TOOL_NAME:
            return t.name, schema_of(t)
    for t in tools:
        if "search" in str(getattr(t, "name", "")).lower():
            return t.name, schema_of(t)
    first = tools[0]
    return getattr(first, "name", PARALLEL_TOOL_NAME), schema_of(first)
