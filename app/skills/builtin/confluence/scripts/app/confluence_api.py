"""Thin wrapper over the Confluence Cloud REST API (OAuth 2.0 / 3LO).

Reads/writes use the v2 API (https://api.atlassian.com/ex/confluence/{cloudId}/wiki/api/v2);
free-text search uses the v1 CQL endpoint (.../wiki/rest/api/search), which has no v2
equivalent yet. All calls use the user's own access token. Page bodies use the
"storage" (XHTML) representation; ``text_to_storage`` wraps plain text into it.
Stdlib ``urllib`` only — no third-party HTTP client.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_ROOT = "https://api.atlassian.com"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"confluence api error {status}: {message}")
        self.status = status
        self.body = body


def text_to_storage(text: str) -> str:
    """Wrap plain text into Confluence 'storage' (XHTML) — one <p> per line."""
    paras = []
    for line in (text or "").split("\n"):
        if line.strip():
            paras.append(f"<p>{html.escape(line)}</p>")
    return "".join(paras) or "<p></p>"


_TAG_RE = re.compile(r"<[^>]+>")


def storage_to_text(value: str) -> str:
    """Best-effort flatten Confluence 'storage' XHTML to plain text."""
    if not value:
        return ""
    text = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _request(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
    max_retries: int = 3,
) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": "cremind-skill/1.0"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code == 429 and attempt < max_retries:
                retry_after = e.headers.get("Retry-After", "1") or "1"
                try:
                    delay = min(int(retry_after), 30)
                except ValueError:
                    delay = 2
                time.sleep(delay)
                attempt += 1
                continue
            try:
                parsed = json.loads(detail) if detail else None
            except ValueError:
                parsed = detail
            raise ApiError(e.code, detail[:300], parsed)
        except (urllib.error.URLError, OSError) as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            raise ApiError(0, str(e))


class ConfluenceClient:
    def __init__(self, access_token: str, cloud_id: str):
        self.access_token = access_token
        self.cloud_id = cloud_id
        self.base_v2 = f"{API_ROOT}/ex/confluence/{cloud_id}/wiki/api/v2"
        self.base_v1 = f"{API_ROOT}/ex/confluence/{cloud_id}/wiki/rest/api"

    def _v2(self, method: str, path: str, **kw) -> Any:
        return _request(method, f"{self.base_v2}{path}", access_token=self.access_token, **kw)

    def _v1(self, method: str, path: str, **kw) -> Any:
        return _request(method, f"{self.base_v1}{path}", access_token=self.access_token, **kw)

    # --- spaces ---

    def spaces(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return self._v2("GET", "/spaces", params={"limit": limit}).get("results", []) or []

    # --- pages ---

    def pages(self, *, space_id: str | None = None, title: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if space_id:
            params["space-id"] = space_id
        if title:
            params["title"] = title
        return self._v2("GET", "/pages", params=params).get("results", []) or []

    def get_page(self, page_id: str, *, body_format: str = "storage") -> dict[str, Any]:
        return self._v2("GET", f"/pages/{urllib.parse.quote(page_id)}", params={"body-format": body_format})

    def create_page(self, *, space_id: str, title: str, body: str) -> dict[str, Any]:
        payload = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": text_to_storage(body)},
        }
        return self._v2("POST", "/pages", body=payload)

    def update_page(self, page_id: str, *, title: str | None = None, body: str | None = None) -> dict[str, Any]:
        current = self.get_page(page_id)
        version = int((current.get("version") or {}).get("number", 1)) + 1
        payload: dict[str, Any] = {
            "id": page_id,
            "status": "current",
            "title": title or current.get("title", ""),
            "version": {"number": version},
        }
        if body is not None:
            payload["body"] = {"representation": "storage", "value": text_to_storage(body)}
        return self._v2("PUT", f"/pages/{urllib.parse.quote(page_id)}", body=payload)

    # --- search (v1 CQL; no v2 equivalent yet) ---

    def search(self, cql: str, *, limit: int = 25) -> list[dict[str, Any]]:
        return self._v1("GET", "/search", params={"cql": cql, "limit": limit}).get("results", []) or []
