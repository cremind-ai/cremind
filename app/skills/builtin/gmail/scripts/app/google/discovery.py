"""Client for the cremind-connect public endpoints.

A single GET to {base}/.well-known/cremind-connect tells the skill which OAuth
client id + scopes to use, which Pub/Sub topic to point users.watch() at, which
Calendar webhook URL to use, and the relay WebSocket URL. A separate GET to
{base}/credentials/<provider> returns the OAuth client id + secret, served
dynamically so the org can rotate them without a client update. This keeps the
skill self-configuring and the relay the single source of truth.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://connect.cremind.io"


class DiscoveryError(RuntimeError):
    pass


class Discovery:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: int = 15, cache_ttl: int = 300):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._doc: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._creds: dict[str, dict[str, Any]] = {}
        self._creds_at: dict[str, float] = {}

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

    def provider(self, provider_id: str = "google") -> dict[str, Any]:
        for p in self.document().get("providers", []):
            if p.get("provider") == provider_id:
                return p
        raise DiscoveryError(f"provider {provider_id!r} not present in discovery doc")

    def client_id(self, provider_id: str = "google") -> str:
        cid = self.provider(provider_id).get("authClientId", "")
        if not cid:
            raise DiscoveryError("discovery doc has no authClientId")
        return cid

    def _credentials_endpoint(self, provider_id: str) -> str:
        return f"{self.base_url}/credentials/{provider_id}"

    def credentials(self, provider_id: str = "google", *, force: bool = False) -> dict[str, Any]:
        """Fetch (and cache) the OAuth client id + secret for a provider.

        Served by cremind-connect at /credentials/<provider> so the org can
        rotate the (non-confidential, Desktop) client credentials centrally.
        """
        now = time.time()
        cached = self._creds.get(provider_id)
        if cached is not None and not force and (now - self._creds_at.get(provider_id, 0.0)) < self.cache_ttl:
            return cached
        req = urllib.request.Request(
            self._credentials_endpoint(provider_id),
            headers={"accept": "application/json", "user-agent": "cremind-skill/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise DiscoveryError(f"failed to fetch credentials from {self._credentials_endpoint(provider_id)}: {e}") from e
        self._creds[provider_id] = doc
        self._creds_at[provider_id] = now
        return doc

    def client_secret(self, provider_id: str = "google") -> str:
        return self.credentials(provider_id).get("clientSecret", "")

    def scopes(self, resource_id: str | None = None, provider_id: str = "google") -> list[str]:
        """OAuth scopes to request.

        With ``resource_id`` ("gmail"/"calendar") return only that resource's
        scopes, so each skill's consent screen asks for just what it needs (least
        privilege). Without it, fall back to the provider-level list (back-compat).

        Returns [] when the chosen entry carries no ``scopes`` key — e.g. an older
        cremind-connect that has not split scopes per resource yet — so the caller
        applies its own per-skill fallback. A missing key never raises here.
        """
        if resource_id is not None:
            return list(self.resource(resource_id, provider_id).get("scopes", []))
        return list(self.provider(provider_id).get("scopes", []))

    def resource(self, resource_id: str, provider_id: str = "google") -> dict[str, Any]:
        for r in self.provider(provider_id).get("resources", []):
            if r.get("resource") == resource_id:
                return r
        raise DiscoveryError(f"resource {resource_id!r} not found for provider {provider_id!r}")

    def gmail_topic(self) -> str:
        topic = self.resource("gmail").get("pubsubTopic", "")
        if not topic:
            raise DiscoveryError("discovery doc has no gmail pubsubTopic")
        return topic

    def webhook_url(self, resource_id: str, provider_id: str = "google") -> str:
        """Push-notification webhook URL for a web_hook-channel resource
        (Calendar, Drive). Raises if the resource carries no ``webhookUrl``."""
        url = self.resource(resource_id, provider_id).get("webhookUrl", "")
        if not url:
            raise DiscoveryError(f"discovery doc has no {resource_id} webhookUrl")
        return url

    def calendar_webhook_url(self) -> str:
        return self.webhook_url("calendar")
