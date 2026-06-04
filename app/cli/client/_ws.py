"""WebSocket client wrapper.

Mirrors `cli/internal/wsclient/wsclient.go`. Authentication is via the
`Sec-WebSocket-Protocol` subprotocol header — the Cremind server reads
`subprotocols[0] == "bearer"` and `subprotocols[1] == <token>` (see
`app/api/processes.py:185`). This matches what the UI does, because the
browser WebSocket constructor cannot set arbitrary headers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from urllib.parse import urlparse, urlunparse

from websockets.asyncio.client import ClientConnection, connect


_HTTP_TO_WS = {"http": "ws", "https": "wss"}


def http_to_ws_url(server: str, path: str) -> str:
    """Translate an `http(s)://host` server base + a `/api/...` path into a
    `ws(s)://host/api/...` URL.
    """
    parsed = urlparse(server)
    new_scheme = _HTTP_TO_WS.get(parsed.scheme, parsed.scheme)
    base = urlunparse(parsed._replace(scheme=new_scheme))
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path


@asynccontextmanager
async def connect_ws(
    server: str,
    path: str,
    token: Optional[str] = None,
    *,
    max_size: int = 4 * 1024 * 1024,
    ping_interval: float = 20.0,
    open_timeout: float = 10.0,
) -> AsyncIterator[ClientConnection]:
    """Open a WebSocket connection to the Cremind server.

    Authentication is via the `Sec-WebSocket-Protocol` subprotocol header.
    Pass `token=None` to skip subprotocol negotiation entirely.
    """
    url = http_to_ws_url(server, path)
    subprotocols = ["bearer", token] if token else None
    async with connect(
        url,
        subprotocols=subprotocols,
        max_size=max_size,
        ping_interval=ping_interval,
        open_timeout=open_timeout,
    ) as ws:
        yield ws
