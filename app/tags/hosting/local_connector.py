"""The in-process connector: how a worker hosted by the backend reaches Cremind.

A worker's code (:mod:`app.tags.runtime`) talks to Cremind through a
:class:`~app.tags.runtime.connector.client.ConnectorClient`. A remote worker
sends HTTP; a worker the backend runs itself gets a :class:`LocalConnector`:
the same class with its transport replaced. Every request is routed through
:data:`app.tags.connector_service.ENDPOINTS` — the table the HTTP API serves —
after the same credential check, plus what only an in-process worker asserts
(:class:`~app.tags.connector_service.WorkerExpectation`: the companion, the
owning profile's immutable UUID, the worker generation, the hardware host).
Answers and refusals then pass through the client's own decoding (a synthetic
``httpx.Response``), so the worker sees exactly the JSON, the round-tripped
types and the exceptions it would see over HTTP: running inside the backend
bypasses no check the HTTP authentication performs.

Loops: the hardware runtime runs its workers on its own event loop (a thread
of its own, so a busy worker never delays the server), while Cremind's
storage belongs to the server's loop. Each request is scheduled there with
:func:`asyncio.run_coroutine_threadsafe` and awaited from the worker's loop;
cancelling the worker's await cancels the server-side coroutine too (a
long-polling ``commands`` call ends with its worker). With no separate loop
(tests), the request simply runs on the caller's.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from typing import Any

import httpx

from app.tags import connector_service as svc
from app.tags.runtime.connector.client import (
    DEFAULT_TIMEOUT_S,
    ConnectorClient,
    ConnectorRejected,
    ConnectorUnavailable,
    Credential,
)
from app.tags.service import TagError
from app.utils.logger import logger

LOCAL_BASE_URL = "cremind-local://in-process"


def _json_round_trip(value: Any) -> Any:
    """What the other side would read after JSON on the wire (tuples become lists, keys strings)."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


async def on_server(coro: Awaitable[Any], server_loop: asyncio.AbstractEventLoop | None, timeout: float) -> Any:
    """Await ``coro`` on the server's loop (where Cremind's storage lives) from the runtime's; cancelling the
    caller cancels it there too. Without a separate server loop it runs right here."""
    if server_loop is None or server_loop is asyncio.get_running_loop():
        return await asyncio.wait_for(coro, timeout)
    future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, timeout), server_loop)
    return await asyncio.wrap_future(future)


class LocalConnector(ConnectorClient):
    """A :class:`ConnectorClient` whose requests run in this process (see the module docstring)."""

    def __init__(self, credential: Credential, *, expect: svc.WorkerExpectation,
                 server_loop: asyncio.AbstractEventLoop | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        # Deliberately not ConnectorClient.__init__: there is no HTTP client and no TLS to set up.
        self.base_url = LOCAL_BASE_URL
        self.credential = credential
        self.ca_file = None
        self.timeout = timeout
        self.expect = expect
        self._server_loop = server_loop
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True

    def __repr__(self) -> str:
        return f"LocalConnector({self.expect.companion_id!r}, {self.credential_id!r})"

    # -- transport ---------------------------------------------------------------

    async def _request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None,
                       timeout: float | None = None) -> Any:
        where = f"{method} {path}"
        if self._closed:
            raise ConnectorUnavailable(f"{where}: the connector is closed")
        found = svc.match(method, path)
        if found is None:
            raise ConnectorRejected(f"{where}: no such connector endpoint", status=404)
        endpoint, path_params = found
        try:
            body = _json_round_trip(json) if json is not None else None
        except (TypeError, ValueError) as exc:
            raise ConnectorRejected(f"{where}: the request body is not JSON ({exc})", status=400) from None
        call = svc.Call(path_params, {k: str(v) for k, v in (params or {}).items()}, body)
        limit = timeout if timeout is not None else self.timeout
        try:
            status, payload = await self._on_server(self._serve(endpoint, call), limit)
        except TimeoutError:
            raise ConnectorUnavailable(f"{where}: timed out") from None
        except RuntimeError as exc:  # the server's loop is closed (Cremind is stopping)
            raise ConnectorUnavailable(f"{where}: Cremind is not running ({exc})") from None
        try:
            response = httpx.Response(status, json=payload)
        except (TypeError, ValueError):
            logger.exception(f"[tags] {where}: the answer is not JSON")
            response = httpx.Response(500, json={"error": "internal_error", "message": "The answer is not JSON."})
        return self._decode(where, response)

    async def _on_server(self, coro: Awaitable[tuple[int, Any]], timeout: float) -> tuple[int, Any]:
        return await on_server(coro, self._server_loop, timeout)

    async def _serve(self, endpoint: svc.Endpoint, call: svc.Call) -> tuple[int, Any]:
        """Runs on the server's loop: the HTTP handler's steps, minus HTTP."""
        try:
            grant = await svc.authenticate(self.credential.credential_id, self.credential.secret, endpoint.kind,
                                           expect=self.expect)
            if endpoint.body and not isinstance(call.body, dict):
                raise TagError(400, "invalid_json", "The request body must be a JSON object.")
            return 200, await endpoint.run(grant, call)
        except TagError as exc:
            return exc.status, {"error": exc.code, "message": exc.message, "detail": exc.message, **exc.extra}
        except Exception:  # noqa: BLE001 - what the HTTP server answers as a 500
            logger.exception(f"[tags] in-process connector {endpoint.method} {endpoint.path} failed")
            return 500, {"error": "internal_error", "message": "Cremind could not handle the request."}


__all__ = ["LOCAL_BASE_URL", "LocalConnector", "on_server"]
