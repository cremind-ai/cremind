"""Web Fetch built-in tool.

Fetches an HTTP(S) URL and extracts readable markdown/text using the standard
library only (regex HTML->markdown, no renderer, no HTML-parser dependency).
Ported from OpenClaw's ``web_fetch`` + its SSRF guard.

Invocation
----------
The reasoning model calls ``fetch_url`` directly via native function calling,
filling ``url`` plus optional ``extract_mode``/``max_chars`` from the tool's
JSON-Schema. There is no per-group routing LLM.

Security: SSRF guard
--------------------
Before fetching (and again after each redirect) the host is resolved with
``socket.getaddrinfo`` and every resolved IP is checked against
private/loopback/link-local/reserved/multicast/metadata ranges. Non-http(s)
schemes are rejected. This closes the SSRF gap (Cremind had no guard before).
Known limitation: a small DNS-rebinding TOCTOU window remains (DNS could change
between the check and httpx's own connect-time resolution); acceptable for a
basic guard.
"""

from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import json
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.external_content import wrap_web_content
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Web Fetch"

FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_MAX_CHARS = 20_000
MAX_MAX_CHARS = 200_000
MIN_MAX_CHARS = 100
MAX_RESPONSE_BYTES = 750_000
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 30.0
CACHE_TTL_SECONDS = 15 * 60

_EXTRACT_MODES = ("markdown", "text")

# Cloud metadata endpoints that must never be reachable via this tool.
_BLOCKED_METADATA_IPS = frozenset({
    "169.254.169.254",   # AWS / GCP / Azure / OpenStack IMDS
    "fd00:ec2::254",     # AWS IMDSv2 over IPv6
})


class Var:
    """Optional config keys for the Web Fetch tool (all have defaults)."""
    MAX_CHARS = "WEB_FETCH_MAX_CHARS"   # default output char cap


TOOL_CONFIG: ToolConfig = {
    "name": "web_fetch",
    "display_name": SERVER_NAME,
    "default_model_group": "low",
    "visible": True,
    "llm_parameters": {
        "tool_instructions": (
            "Fetch a single web page by URL and return its readable content as "
            "markdown or plain text. Lightweight page access -- no browser "
            "automation, no JavaScript. Give it the URL to read; optionally ask "
            "for plain text or a smaller character limit."
        ),
    },
    "required_config": {
        Var.MAX_CHARS: {
            "description": (
                "Default maximum characters of page content to return when the "
                "agent does not specify max_chars."
            ),
            "type": "number",
            "default": DEFAULT_MAX_CHARS,
        },
    },
    # The model fills url/extract_mode/max_chars directly via native function calling.
}


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------

_FETCH_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _cache_key(url: str, extract_mode: str, max_chars: int) -> str:
    return "\x1f".join([url, extract_mode, str(max_chars)])


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _FETCH_CACHE.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if time.monotonic() >= expires_at:
        _FETCH_CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: str, payload: Dict[str, Any]) -> None:
    _FETCH_CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, payload)


# ---------------------------------------------------------------------------
# HTML -> markdown / text (pure functions -- unit-tested without network)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def decode_entities(value: str) -> str:
    return _html.unescape(value)


def strip_tags(value: str) -> str:
    return decode_entities(_TAG_RE.sub("", value))


def normalize_whitespace(value: str) -> str:
    """Collapse display whitespace while preserving paragraph breaks."""
    value = value.replace("\r", "")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_NOSCRIPT_RE = re.compile(r"<noscript[^>]*>.*?</noscript>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r"""<a\s+[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
_HEADING_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_BR_HR_RE = re.compile(r"<(br|hr)\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(
    r"</(p|div|section|article|header|footer|table|tr|ul|ol)>", re.IGNORECASE,
)


def html_to_markdown(html_text: str) -> Tuple[str, Optional[str]]:
    """Convert lightweight HTML to coarse markdown. Returns ``(text, title)``."""
    title_match = _TITLE_RE.search(html_text)
    title = normalize_whitespace(strip_tags(title_match.group(1))) if title_match else None

    text = _SCRIPT_RE.sub("", html_text)
    text = _STYLE_RE.sub("", text)
    text = _NOSCRIPT_RE.sub("", text)

    def _anchor(m: "re.Match[str]") -> str:
        href, body = m.group(1), m.group(2)
        label = normalize_whitespace(strip_tags(body))
        return f"[{label}]({href})" if label else href

    text = _ANCHOR_RE.sub(_anchor, text)

    def _heading(m: "re.Match[str]") -> str:
        level = max(1, min(6, int(m.group(1))))
        label = normalize_whitespace(strip_tags(m.group(2)))
        return f"\n{'#' * level} {label}\n"

    text = _HEADING_RE.sub(_heading, text)

    def _li(m: "re.Match[str]") -> str:
        label = normalize_whitespace(strip_tags(m.group(1)))
        return f"\n- {label}" if label else ""

    text = _LI_RE.sub(_li, text)
    text = _BR_HR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = strip_tags(text)
    text = normalize_whitespace(text)
    return text, (title or None)


_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_ORDERED_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)


def markdown_to_text(markdown: str) -> str:
    """Strip markdown decoration for plain-text extraction."""
    text = _MD_IMAGE_RE.sub("", markdown)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_FENCE_RE.sub(lambda m: m.group(0).replace("```", ""), text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_BULLET_RE.sub("", text)
    text = _MD_ORDERED_RE.sub("", text)
    return normalize_whitespace(text)


def truncate_text(value: str, max_chars: int) -> Tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


# ---------------------------------------------------------------------------
# SSRF guard (pure helpers -- unit-tested without network)
# ---------------------------------------------------------------------------

class SsrfBlockedError(Exception):
    """Raised when a URL resolves to a disallowed (private/internal) address."""


def is_blocked_ip(ip: str) -> bool:
    """True when ``ip`` is in a private/loopback/link-local/reserved/metadata range."""
    if ip in _BLOCKED_METADATA_IPS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # un-parseable -> fail closed
    # IPv4-mapped / 6to4 embedded IPv4 -> evaluate the inner address too.
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return is_blocked_ip(str(addr.ipv4_mapped))
        if addr.sixtofour is not None:
            return is_blocked_ip(str(addr.sixtofour))
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_url_scheme(url: str) -> str:
    """Return the host for an http(s) URL, or raise :class:`SsrfBlockedError`."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise SsrfBlockedError("Only http and https URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise SsrfBlockedError("URL has no host.")
    return host


async def assert_host_allowed(host: str) -> None:
    """Resolve ``host`` and block when ANY resolved IP is internal.

    Runs ``getaddrinfo`` in a thread so the event loop is never blocked.
    """
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False

    if literal:
        if is_blocked_ip(host):
            raise SsrfBlockedError(f"Address {host} is in a blocked range.")
        return

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise SsrfBlockedError(f"Could not resolve host '{host}': {exc}") from exc

    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise SsrfBlockedError(f"Host '{host}' did not resolve to any address.")
    for ip in resolved:
        if is_blocked_ip(ip):
            raise SsrfBlockedError(f"Host '{host}' resolves to a blocked address ({ip}).")


# ---------------------------------------------------------------------------
# Body read with byte cap
# ---------------------------------------------------------------------------

async def _read_capped_body(resp: httpx.Response, max_bytes: int) -> Tuple[str, bool]:
    """Stream the response body, truncating at ``max_bytes``."""
    chunks: List[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        if total + len(chunk) > max_bytes:
            chunks.append(chunk[: max_bytes - total])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b"".join(chunks)
    encoding = resp.encoding or "utf-8"
    try:
        return raw.decode(encoding, errors="replace"), truncated
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace"), truncated


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class WebFetchTool(BuiltInTool):
    name: str = "fetch_url"
    description: str = (
        "Fetch a single HTTP(S) URL and return its readable content as markdown "
        "(default) or plain text. No browser automation; JavaScript is not "
        "executed."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http(s) URL to fetch.",
            },
            "extract_mode": {
                "type": "string",
                "description": "Return readable 'markdown' (default) or plain 'text'.",
                "enum": list(_EXTRACT_MODES),
                "default": "markdown",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    f"Maximum characters to return (default {DEFAULT_MAX_CHARS}). "
                    "Content beyond the limit is truncated."
                ),
                "minimum": MIN_MAX_CHARS,
                "maximum": MAX_MAX_CHARS,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        url = (arguments.get("url") or "").strip()
        if not url:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "A 'url' is required.",
            })

        extract_mode = (arguments.get("extract_mode") or "markdown").strip().lower()
        if extract_mode not in _EXTRACT_MODES:
            extract_mode = "markdown"

        variables = arguments.get("_variables") or {}
        try:
            default_max = int(variables.get(Var.MAX_CHARS) or DEFAULT_MAX_CHARS)
        except (TypeError, ValueError):
            default_max = DEFAULT_MAX_CHARS
        try:
            max_chars = int(arguments.get("max_chars") or default_max)
        except (TypeError, ValueError):
            max_chars = default_max
        max_chars = max(MIN_MAX_CHARS, min(max_chars, MAX_MAX_CHARS))

        # Scheme + first SSRF check before any network I/O.
        try:
            host = validate_url_scheme(url)
            await assert_host_allowed(host)
        except SsrfBlockedError as exc:
            return BuiltInToolResult(structured_content={
                "error": "BlockedURL",
                "message": str(exc),
            })

        cache_key = _cache_key(url, extract_mode, max_chars)
        cached = _cache_get(cache_key)
        if cached is not None:
            return BuiltInToolResult(structured_content={**cached, "cached": True})

        headers = {
            "Accept": "text/markdown, text/html;q=0.9, */*;q=0.1",
            "User-Agent": FETCH_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        started = time.monotonic()
        try:
            payload = await self._fetch(url, headers, extract_mode, max_chars)
        except SsrfBlockedError as exc:
            return BuiltInToolResult(structured_content={
                "error": "BlockedURL",
                "message": str(exc),
            })
        except httpx.TimeoutException:
            return BuiltInToolResult(structured_content={
                "error": "Timeout",
                "message": (
                    f"The server did not respond within {int(FETCH_TIMEOUT_SECONDS)}s."
                ),
            })
        except httpx.HTTPError as exc:
            return BuiltInToolResult(structured_content={
                "error": "NetworkError",
                "message": f"Failed to fetch the URL: {exc}",
            })

        if "error" in payload:
            return BuiltInToolResult(structured_content=payload)

        payload["took_ms"] = int((time.monotonic() - started) * 1000)
        _cache_set(cache_key, payload)
        logger.info(
            f"[web_fetch] {url[:80]} -> {payload.get('extractor')} "
            f"(status={payload.get('status')})"
        )
        return BuiltInToolResult(structured_content=payload)

    async def _fetch(
        self, url: str, headers: Dict[str, str], extract_mode: str, max_chars: int,
    ) -> Dict[str, Any]:
        """Manual redirect loop so each hop is re-validated against the SSRF guard."""
        current = url
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False,
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                async with client.stream("GET", current, headers=headers) as resp:
                    # follow_redirects=False means httpx does NOT build
                    # resp.next_request, so resolve the Location header manually.
                    location = resp.headers.get("location") if resp.is_redirect else None
                    if location:
                        next_url = urljoin(current, location)
                        # Re-validate the redirect target BEFORE following it.
                        next_host = validate_url_scheme(next_url)
                        await assert_host_allowed(next_host)
                        current = next_url
                        continue
                    return await self._handle_response(resp, url, current, extract_mode, max_chars)
        return {
            "error": "TooManyRedirects",
            "message": f"Exceeded {MAX_REDIRECTS} redirects.",
            "url": url,
        }

    async def _handle_response(
        self,
        resp: httpx.Response,
        request_url: str,
        final_url: str,
        extract_mode: str,
        max_chars: int,
    ) -> Dict[str, Any]:
        if resp.status_code >= 400:
            return {
                "error": "UpstreamError",
                "message": f"Server returned HTTP {resp.status_code}.",
                "status": resp.status_code,
                "url": request_url,
                "final_url": final_url,
            }

        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        body, body_truncated = await _read_capped_body(resp, MAX_RESPONSE_BYTES)

        title: Optional[str] = None
        extractor = "raw"
        text = body

        if content_type in ("text/markdown", "text/x-markdown"):
            extractor = "markdown"
            if extract_mode == "text":
                text = markdown_to_text(body)
        elif content_type in ("text/html", "application/xhtml+xml"):
            md, title = html_to_markdown(body)
            text = markdown_to_text(md) if extract_mode == "text" else md
            extractor = "html"
        elif content_type == "application/json":
            try:
                text = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                extractor = "json"
            except (json.JSONDecodeError, ValueError):
                text = body
                extractor = "raw"
        # else: leave raw text as-is.

        text, char_truncated = truncate_text(text, max_chars)

        warnings: List[str] = []
        if body_truncated:
            warnings.append(f"Response body truncated after {MAX_RESPONSE_BYTES} bytes.")
        if char_truncated:
            warnings.append(f"Output truncated to {max_chars} characters.")

        return {
            "url": request_url,
            "final_url": final_url,
            "status": resp.status_code,
            "content_type": content_type or "application/octet-stream",
            "title": (wrap_web_content(title, source="web_fetch") if title else None),
            "extract_mode": extract_mode,
            "extractor": extractor,
            "external_content": {
                "untrusted": True,
                "source": "web_fetch",
                "wrapped": True,
            },
            "truncated": bool(body_truncated or char_truncated),
            "length": len(text),
            # Wrap the page body so prompt injection from the fetched site is
            # delimited + flagged for the Reasoning Agent.
            "text": wrap_web_content(text, source="web_fetch"),
            "warning": "\n".join(warnings) if warnings else None,
        }


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [WebFetchTool()]
