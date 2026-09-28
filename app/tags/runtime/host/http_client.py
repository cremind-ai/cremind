"""A desktop gateway computer's way to Cremind: ``/api/tag-host/v1`` over HTTPS.

The host agent's :class:`~app.tags.runtime.host.agent.HostClient` for a
computer enrolled with a Cremind server elsewhere. Every request carries the
computer's host credential (``Authorization: CremindHost <id>.<secret>``,
scoped to this computer and one profile); the computer only ever connects
out — the work long-poll replaces any inbound port.

TLS: when the enrollment pinned the server's own CA (a Cremind with a private
certificate authority), exactly that CA is trusted; otherwise the system
trust store decides. Refusals keep the server's status and error code
(:class:`~app.tags.runtime.host.agent.HostError`); a revoked or unknown
credential is a 401, which ends the host (it was removed).
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx

from .agent import HostError

PREFIX = "/api/tag-host/v1"
TIMEOUT_S = 20.0


class HttpHostClient:
    """See the module docstring."""

    def __init__(self, server: str, authorization: str, *, ca_pem: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = TIMEOUT_S) -> None:
        self.server = server.rstrip("/")
        verify: ssl.SSLContext | bool = True
        if ca_pem and self.server.startswith("https://") and transport is None:
            verify = ssl.create_default_context(cadata=ca_pem)
        self._client = httpx.AsyncClient(
            base_url=self.server, verify=verify, transport=transport, timeout=timeout, follow_redirects=False,
            headers={"Authorization": authorization, "User-Agent": "cremind-host"})

    async def _call(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        try:
            response = await self._client.request(method, PREFIX + path, json=body, params=params,
                                                  timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException:
            raise HostError(f"Cremind at {self.server} did not answer in time.", code="timeout") from None
        except httpx.HTTPError as exc:
            raise HostError(f"Cremind at {self.server} could not be reached ({type(exc).__name__}).",
                            code="unreachable") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code >= 400:
            raise HostError(str(data.get("message") or f"Cremind answered {response.status_code}."),
                            status=response.status_code, code=str(data.get("error") or "") or None)
        return data

    async def hello(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/hello", body=body)

    async def work(self, wait: int) -> list[dict[str, Any]]:
        answer = await self._call("GET", "/work", params={"wait": int(wait)}, timeout=float(wait) + TIMEOUT_S)
        return list(answer.get("work") or [])

    async def progress(self, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", f"/operations/{op_id}/progress", body=body)

    async def register_worker(self, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", f"/operations/{op_id}/worker", body=body)

    async def adopt_worker(self, companion_id: str) -> dict[str, Any]:
        return await self._call("POST", f"/workers/{companion_id}/adopt", body={})

    async def leave(self) -> dict[str, Any]:
        return await self._call("POST", "/leave", body={})

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["HttpHostClient"]
