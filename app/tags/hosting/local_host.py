"""The backend's own hardware host reaching Cremind in process.

The host agent (:mod:`app.tags.runtime.host.agent`) talks to Cremind through a
host client. A desktop host sends HTTPS with its host credential
(``/api/tag-host/v1``); the backend's own host calls the same functions of
:mod:`app.tags.hosts` directly, as the ``server`` host principal, on the
server's loop. Answers and refusals keep their JSON shape and error codes, so
the agent cannot tell the two apart.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.tags import hosts
from app.tags.runtime.host.agent import HostError
from app.tags.service import TagError

from .local_connector import _json_round_trip, on_server

TIMEOUT_S = 30.0


class LocalHostClient:
    """The server host's :class:`~app.tags.runtime.host.agent.HostClient`."""

    def __init__(self, host_id: str, server_loop: asyncio.AbstractEventLoop | None) -> None:
        self.principal = hosts.HostPrincipal(host_id, hosts.SERVER)
        self._server_loop = server_loop

    async def _call(self, coro: Any, timeout: float = TIMEOUT_S) -> Any:
        async def guarded() -> Any:
            try:
                return _json_round_trip(await coro)
            except TagError as exc:
                raise HostError(exc.message, status=exc.status, code=exc.code) from None

        try:
            return await on_server(guarded(), self._server_loop, timeout)
        except TimeoutError:
            raise HostError("Cremind did not answer in time.", status=504, code="timeout") from None

    async def hello(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call(hosts.host_hello(self.principal, _json_round_trip(body)))

    async def work(self, wait: int) -> list[dict[str, Any]]:
        answer = await self._call(hosts.host_work(self.principal, wait), timeout=wait + TIMEOUT_S)
        return list(answer.get("work") or [])

    async def progress(self, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call(hosts.host_progress(self.principal, op_id, _json_round_trip(body)))

    async def register_worker(self, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call(hosts.host_register_worker(self.principal, op_id, _json_round_trip(body)))

    async def aclose(self) -> None:
        return None


__all__ = ["LocalHostClient"]
