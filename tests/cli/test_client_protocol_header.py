"""The CLI sends the client-protocol marker on every request it makes.

The server refuses tool-configuration, setup and cleanup writes without
``X-Cremind-Client-Protocol: 2`` (tool ids changed meaning in the search-tool
rename). Every command goes through :class:`app.cli.client._base.Client`, so
the marker lives on both of its httpx clients: the 60-second one and the
no-timeout one behind SSE streams, downloads and long waits. This drives each
request shape through a mock transport and reads the header off the wire.
"""

from __future__ import annotations

import asyncio
import io

import httpx
import pytest

from app.cli.client import _base
from app.cli.client._base import CLIENT_PROTOCOL_HEADER, CLIENT_PROTOCOL_VERSION, APIError, Client
from app.cli.config import Config


@pytest.fixture
def wire(monkeypatch) -> list[httpx.Request]:
    """Every request any Client sends, answered by a stub server."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b'data: {"type": "done"}\n\n')
        if request.url.path == "/api/clean" and CLIENT_PROTOCOL_HEADER.lower() not in request.headers:
            return httpx.Response(426, json={"error": "ClientUpgradeRequired", "message": "update"})
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(_base.httpx, "AsyncClient", factory)
    return seen


def _cfg(token: str = "tok") -> Config:
    return Config(server="http://cremind.test", token=token, output="table", no_color=True)


async def _exercise(client: Client) -> None:
    await client.get_json("/api/tools")
    await client.get_json_status("/health")
    await client.get_bytes("/api/config/export")
    await client.post_json("/api/clean", {"components": ["documentation_search"]})
    await client.put_json("/api/tools/documentation_search/enabled", {"enabled": False})
    await client.patch_json("/api/tools/x", {})
    await client.delete("/api/tools/x")
    await client.upload("/api/files/upload", files=[("file", ("a.txt", b"hi"))])
    await client.download("/api/files/a.txt", io.BytesIO())
    async for _ in client.stream("/api/documentation-search/stream"):
        pass
    async for _ in client.stream_post("/api/features/stream", {"features": []}):
        pass


def test_every_request_shape_carries_the_marker(wire):
    async def run():
        async with Client(_cfg()) as client:
            await _exercise(client)

    asyncio.run(run())

    assert len(wire) == 11
    for request in wire:
        assert request.headers[CLIENT_PROTOCOL_HEADER] == str(CLIENT_PROTOCOL_VERSION), request.url
        assert request.headers["authorization"] == "Bearer tok"


def test_the_marker_is_sent_without_a_token_too(wire):
    """``cremind setup complete`` posts /api/config/setup before any token
    exists — the gated setup route must still see a current client."""
    async def run():
        async with Client(_cfg(token=""), timeout=None) as client:
            await client.post_json("/api/config/setup", {"profile": "alice"})

    asyncio.run(run())

    assert wire[0].headers[CLIENT_PROTOCOL_HEADER] == str(CLIENT_PROTOCOL_VERSION)
    assert "authorization" not in wire[0].headers


def test_a_426_surfaces_its_explanation(monkeypatch):
    """What an operator sees when a server refuses the client anyway."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(426, json={"error": "ClientUpgradeRequired",
                                         "message": "Update the web UI / CLI (pip install -U cremind) and retry."})

    real = httpx.AsyncClient
    monkeypatch.setattr(_base.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    async def run():
        async with Client(_cfg()) as client:
            await client.post_json("/api/clean", {})

    with pytest.raises(APIError) as caught:
        asyncio.run(run())
    assert caught.value.status == 426
    assert "ClientUpgradeRequired" in str(caught.value)
    assert "pip install -U cremind" in str(caught.value)
