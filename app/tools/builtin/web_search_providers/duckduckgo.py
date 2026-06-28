"""DuckDuckGo HTML-scraping web-search provider (EXPERIMENTAL / UNOFFICIAL).

Why this is OPT-IN and off by default
--------------------------------------
``html.duckduckgo.com`` is NOT an official API. Its ``robots.txt`` is
``Disallow: /``, it is aggressively IP-rate-limited (HTTP 202/403, CAPTCHA),
breaks frequently, and is in tension with DuckDuckGo's Acceptable Use Policy.
SERP scraping is also actively litigated (2025-2026). The ``web_search`` tool
therefore reaches this provider only when the user explicitly sets
``WEB_SEARCH_PROVIDER=duckduckgo`` or opts into ``auto`` fallback.

Conservative posture (intentional): a SINGLE best-effort request, no IP
rotation, no anti-bot circumvention. On 202/403/CAPTCHA we raise a clear error
and let the in-process TTL cache (in the tool) absorb repeats. Do NOT add
retry/rotation here -- defeating bot-detection is exactly the conduct framed as
DMCA anti-circumvention in the current SERP-scraping lawsuits.
"""

from __future__ import annotations

import html as _html
import re
import urllib.parse
from typing import Any, Dict, List

import httpx

from app.tools.builtin.web_search_providers.base import (
    ProviderError,
    SearchResult,
)
from app.utils.logger import logger

PROVIDER_ID = "duckduckgo"

DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html"
DDG_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
SEARCH_TIMEOUT_SECONDS = 20.0

# safe_search value -> DuckDuckGo ``kp`` query param.
_SAFE_SEARCH_PARAM: Dict[str, str] = {"strict": "1", "moderate": "-1", "off": "-2"}
DEFAULT_SAFE_SEARCH = "moderate"

# Config keys (read from the tool's ``_variables``; all optional).
VAR_REGION = "DDG_REGION"
VAR_SAFE_SEARCH = "DDG_SAFE_SEARCH"


# ---------------------------------------------------------------------------
# Pure parsing helpers (unit-tested without network)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# ``result__a`` anchor (title + href) and ``result__snippet`` anchor.
_RESULT_A_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")([^>]*)>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_NEXT_RESULT_A_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")[^>]*>',
    re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__snippet\b[^"]*")[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
_BOT_MARKERS_RE = re.compile(
    r'g-recaptcha|are you a human|id="challenge-form"|name="challenge"',
    re.IGNORECASE,
)


def decode_html_entities(text: str) -> str:
    """Decode HTML entities (named, numeric ``&#NNN;``, hex ``&#xNN;``)."""
    return _html.unescape(text)


def strip_html(fragment: str) -> str:
    """Remove tags, decode entities, collapse whitespace."""
    text = _TAG_RE.sub(" ", fragment)
    text = decode_html_entities(text)
    return _WS_RE.sub(" ", text).strip()


def decode_ddg_url(raw_url: str) -> str:
    """Resolve a DuckDuckGo redirect (``//duckduckgo.com/l/?uddg=...``).

    DDG wraps every result href in a redirect carrying the real URL in the
    percent-encoded ``uddg`` query param. Extract + decode it; pass a direct
    link through unchanged.
    """
    if not raw_url:
        return raw_url
    normalized = ("https:" + raw_url) if raw_url.startswith("//") else raw_url
    try:
        parsed = urllib.parse.urlparse(normalized)
        params = urllib.parse.parse_qs(parsed.query)
        uddg = params.get("uddg", [None])[0]
        if uddg:
            return uddg  # parse_qs already percent-decoded it
    except ValueError:
        pass
    return raw_url


def is_bot_challenge(html_text: str) -> bool:
    """True when DDG served an anti-bot page instead of results.

    If a ``result__a`` anchor is present the page is a real SERP; otherwise
    look for known challenge markers.
    """
    if _RESULT_A_RE.search(html_text):
        return False
    return bool(_BOT_MARKERS_RE.search(html_text))


def parse_ddg_html(html_text: str) -> List[SearchResult]:
    """Parse DuckDuckGo HTML into ``[{title, url, snippet}]``.

    For each ``result__a`` anchor, scope the snippet search to the HTML between
    this anchor and the next ``result__a`` so snippets pair with the right hit.
    """
    results: List[SearchResult] = []
    for match in _RESULT_A_RE.finditer(html_text):
        raw_attrs = match.group(1) or ""
        raw_title = match.group(2) or ""
        href_match = _HREF_RE.search(raw_attrs)
        raw_href = href_match.group(1) if href_match else ""

        trailing = html_text[match.end():]
        next_match = _NEXT_RESULT_A_RE.search(trailing)
        scoped = trailing[: next_match.start()] if next_match else trailing
        snippet_match = _SNIPPET_RE.search(scoped)
        raw_snippet = snippet_match.group(1) if snippet_match else ""

        title = strip_html(raw_title)
        url = decode_ddg_url(decode_html_entities(raw_href))
        snippet = strip_html(raw_snippet)

        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class DuckDuckGoProvider:
    """Keyless, experimental DuckDuckGo HTML-scrape provider."""

    id = PROVIDER_ID

    async def search(
        self, query: str, *, count: int, variables: Dict[str, Any],
    ) -> List[SearchResult]:
        region = (variables.get(VAR_REGION) or "").strip()
        safe_search = (variables.get(VAR_SAFE_SEARCH) or DEFAULT_SAFE_SEARCH).strip().lower()
        if safe_search not in _SAFE_SEARCH_PARAM:
            safe_search = DEFAULT_SAFE_SEARCH

        params = {"q": query, "kp": _SAFE_SEARCH_PARAM[safe_search]}
        if region:
            params["kl"] = region

        try:
            async with httpx.AsyncClient(
                timeout=SEARCH_TIMEOUT_SECONDS, follow_redirects=True,
            ) as client:
                resp = await client.get(
                    DDG_HTML_ENDPOINT,
                    params=params,
                    headers={"User-Agent": DDG_USER_AGENT},
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"DuckDuckGo did not respond within {int(SEARCH_TIMEOUT_SECONDS)}s.",
                code="Timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"DuckDuckGo request failed: {exc}", code="Network") from exc

        # 202 is DuckDuckGo's signature rate-limit response for scrapers.
        if resp.status_code == 202:
            raise ProviderError(
                "DuckDuckGo rate-limited the request (HTTP 202). Reduce "
                "frequency or use the 'parallel' provider.",
                code="RateLimited",
            )
        if resp.status_code != 200:
            raise ProviderError(
                f"DuckDuckGo returned HTTP {resp.status_code}.", code="Upstream",
            )

        html_text = resp.text
        if is_bot_challenge(html_text):
            raise ProviderError(
                "DuckDuckGo served an anti-bot challenge instead of results.",
                code="BotChallenge",
            )

        results = parse_ddg_html(html_text)[:count]
        logger.info(f"[web_search:duckduckgo] '{query[:60]}' -> {len(results)} results")
        return results
