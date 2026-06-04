"""HTTP client for the Cremind server.

Mirrors `cli/internal/client/client.go`. All public methods are async; commands
should use `async with Client(cfg) as client:` so the underlying connection
pools are closed properly.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

import httpx

from app.cli.config import Config


class APIError(Exception):
    """Raised when the server responds with a non-2xx status."""

    def __init__(self, status: int, body: str = "", raw: bytes = b"") -> None:
        self.status = status
        self.body = body
        self.raw = raw
        super().__init__(self._format())

    def _format(self) -> str:
        if self.body:
            return f"server returned {self.status}: {self.body}"
        return f"server returned {self.status}"


class Client:
    """Async HTTP client for the Cremind server.

    Holds two underlying `httpx.AsyncClient` instances:

    * `_http` — 60s timeout, used for every request/response round-trip.
    * `_stream_http` — no timeout, used for SSE (`stream`) and any other
      long-lived response body. Cancellation flows via the asyncio task.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        headers: dict[str, str] = {}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        self._http = httpx.AsyncClient(
            base_url=cfg.server,
            timeout=60.0,
            headers=headers,
        )
        self._stream_http = httpx.AsyncClient(
            base_url=cfg.server,
            timeout=None,
            headers=headers,
        )

    @property
    def cfg(self) -> Config:
        return self._cfg

    @property
    def server(self) -> str:
        return self._cfg.server

    @property
    def token(self) -> str:
        return self._cfg.token

    @property
    def stream_http(self) -> httpx.AsyncClient:
        """The no-timeout client. Used by `_sse.stream_events`."""
        return self._stream_http

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()
        await self._stream_http.aclose()

    @staticmethod
    def _check_response(resp: httpx.Response) -> None:
        """Raise APIError if status >= 400; mirror Go's APIError shape."""
        if resp.status_code < 400:
            return
        raw = resp.content
        body = ""
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict) and isinstance(decoded.get("error"), str):
                body = decoded["error"]
            else:
                body = raw.decode(errors="replace").strip()
        raise APIError(status=resp.status_code, body=body, raw=raw)

    async def get_json(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        resp = await self._http.get(path, params=params)
        self._check_response(resp)
        if not resp.content:
            return None
        return resp.json()

    async def post_json(
        self,
        path: str,
        body: Any = None,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        return await self._body_request("POST", path, body, params=params)

    async def put_json(
        self,
        path: str,
        body: Any = None,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        return await self._body_request("PUT", path, body, params=params)

    async def patch_json(
        self,
        path: str,
        body: Any = None,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        return await self._body_request("PATCH", path, body, params=params)

    async def delete(
        self,
        path: str,
        body: Any = None,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        return await self._body_request("DELETE", path, body, params=params)

    async def _body_request(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if body is not None:
            kwargs["json"] = body
        resp = await self._http.request(method, path, **kwargs)
        self._check_response(resp)
        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return None

    def stream(self, path: str) -> "AsyncIterator[Event]":  # noqa: F821
        """Open an SSE stream and yield decoded events.

        Lazy import keeps `httpx_sse` out of the cold path for non-streaming
        commands.
        """
        from app.cli.client._sse import stream_events

        return stream_events(self._stream_http, path)
