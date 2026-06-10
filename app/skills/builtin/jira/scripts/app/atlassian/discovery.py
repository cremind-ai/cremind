"""Client for the cremind-connect public discovery endpoint.

A single GET to {base}/.well-known/cremind-connect tells the skill which OAuth
client id + per-resource scopes to use, which Jira webhook URL to register dynamic
webhooks against, and the relay WebSocket URL. Unlike the Google skills, NO client
secret is fetched here — Atlassian's secret is confidential and stays on the
backend, which mediates the token exchange.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://connect.cremind.io"
PROVIDER = "atlassian"


class DiscoveryError(RuntimeError):
    pass


class Discovery:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: int = 15, cache_ttl: int = 300):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._doc: dict[str, Any] | None = None
        self._fetched_at = 0.0

    def _endpoint(self) -> str:
        return f"{self.base_url}/.well-known/cremind-connect"

    def document(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if self._doc is not None and not force and (now - self._fetched_at) < self.cache_ttl:
            return self._doc
        req = urllib.request.Request(
            self._endpoint(),
            headers={"accept": "application/json", "user-agent": "cremind-skill/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise DiscoveryError(f"failed to fetch discovery doc from {self._endpoint()}: {e}") from e
        self._fetched_at = now
        return self._doc

    def relay(self) -> dict[str, Any]:
        return self.document().get("relay", {}) or {}

    def ws_url(self) -> str:
        url = self.relay().get("wsUrl")
        if url:
            return url
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}/subscribe"

    def provider(self, provider_id: str = PROVIDER) -> dict[str, Any]:
        for p in self.document().get("providers", []):
            if p.get("provider") == provider_id:
                return p
        raise DiscoveryError(f"provider {provider_id!r} not present in discovery doc")

    def client_id(self, provider_id: str = PROVIDER) -> str:
        return self.provider(provider_id).get("authClientId", "")

    def resource(self, resource_id: str, provider_id: str = PROVIDER) -> dict[str, Any]:
        for r in self.provider(provider_id).get("resources", []):
            if r.get("resource") == resource_id:
                return r
        raise DiscoveryError(f"resource {resource_id!r} not found for provider {provider_id!r}")

    def scopes(self, resource_id: str, provider_id: str = PROVIDER) -> list[str]:
        """Per-resource OAuth scopes (least privilege). [] if not split per resource."""
        return list(self.resource(resource_id, provider_id).get("scopes", []))

    def webhook_url(self, resource_id: str, provider_id: str = PROVIDER) -> str:
        url = self.resource(resource_id, provider_id).get("webhookUrl", "")
        if not url:
            raise DiscoveryError(f"discovery doc has no webhookUrl for {resource_id!r}")
        return url
