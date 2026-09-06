"""Exercise real HTTP/1, HTTP/2 TLS and plaintext on the same TCP port."""
import asyncio
import socket
import ssl

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from websockets.asyncio.client import connect

from app.config.settings import BaseConfig
from app.config.tls_auto import ensure_local_tls
from app.server import _mk_hypercorn_config
from app.system import tls_listener
from app.system.tls_listener import HTTP_HEADER_LIMIT, PrivateRelayApp, serve_with_http_recovery


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_one_port_preserves_tls_alpn_peer_and_rejects_plaintext_api(monkeypatch, tmp_path, host):
    asyncio.run(_exercise_listener(monkeypatch, tmp_path, host))


async def _exercise_listener(monkeypatch, tmp_path, host):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE_PASSWORD", "")
    monkeypatch.delenv("CREMIND_TLS_TERMINATION", raising=False)
    cert, key = ensure_local_tls(str(tmp_path), ["localhost"])
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
            probe.bind((host, 0))
            port = probe.getsockname()[1]
    except OSError:
        if ":" in host:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    async def endpoint(request):
        return JSONResponse({"peer": request.client.host, "scheme": request.url.scheme})
    async def websocket(websocket):
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()
    async def events(_request):
        async def body():
            yield b"data: first\n\n"
            await asyncio.sleep(0.02)
            yield b"data: second\n\n"
        return StreamingResponse(body(), media_type="text/event-stream")
    app = Starlette(routes=[
        Route("/api/check", endpoint),
        Route("/events", events),
        WebSocketRoute("/ws", websocket),
    ])
    config = _mk_hypercorn_config(host, port, cert, key)
    config.graceful_timeout = 0.1
    stop = asyncio.Event()
    task = asyncio.create_task(serve_with_http_recovery(app, config, shutdown_trigger=stop.wait))
    ssl_context = ssl.create_default_context(cafile=str(tmp_path / "tls" / "ca.pem"))
    try:
        async with httpx.AsyncClient(verify=ssl_context, http2=True, trust_env=False) as client:
            for _ in range(100):
                try:
                    response = await client.get(f"https://{authority}/api/check")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.02)
            else:
                raise AssertionError("TLS listener never became ready")
            assert response.json() == {"peer": host, "scheme": "https"}
            assert response.http_version == "HTTP/2"
            async with client.stream("GET", f"https://{authority}/events") as stream:
                lines = [line async for line in stream.aiter_lines() if line]
                assert stream.http_version == "HTTP/2"
                assert lines == ["data: first", "data: second"]
            plain = await client.get(f"http://{authority}/")
            assert plain.status_code == 200 and "Opening Cremind over HTTPS" in plain.text
            assert plain.headers["connection"] == "close"
            assert (await client.post(f"http://{authority}/api/check")).status_code == 426

        # A request-side TCP half-close still has to receive the complete
        # recovery document. Cancelling both pumps on the first EOF loses this
        # response on real proxies and on clients that explicitly half-close.
        plain_reader, plain_writer = await asyncio.open_connection(host, port)
        plain_writer.write(
            f"GET / HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode()
        )
        await plain_writer.drain()
        plain_writer.write_eof()
        raw_response = await asyncio.wait_for(plain_reader.read(), 2)
        assert b"200" in raw_response.split(b"\r\n", 1)[0]
        assert b"Opening Cremind over HTTPS" in raw_response
        plain_writer.close()
        await plain_writer.wait_closed()

        # Classification reads a complete plaintext header under an absolute
        # deadline and a byte cap. Neither slow-drip nor oversized clients get
        # a private upstream connection or an application response.
        original_timeout = tls_listener.HTTP_HEADER_TIMEOUT
        tls_listener.HTTP_HEADER_TIMEOUT = 0.05
        slow_writer = None
        try:
            slow_reader, slow_writer = await asyncio.open_connection(host, port)
            slow_writer.write(b"G")
            await slow_writer.drain()
            assert await asyncio.wait_for(slow_reader.read(), 1) == b""
        finally:
            tls_listener.HTTP_HEADER_TIMEOUT = original_timeout
            if slow_writer is not None:
                slow_writer.close()
                await slow_writer.wait_closed()

        large_reader, large_writer = await asyncio.open_connection(host, port)
        large_writer.write(b"GET / HTTP/1.1\r\nX-Fill: " + b"x" * HTTP_HEADER_LIMIT)
        await large_writer.drain()
        assert await asyncio.wait_for(large_reader.read(), 1) == b""
        large_writer.close()
        await large_writer.wait_closed()

        websocket_context = ssl.create_default_context(cafile=str(tmp_path / "tls" / "ca.pem"))
        websocket_context.set_alpn_protocols(["http/1.1"])
        async with connect(f"wss://{authority}/ws", ssl=websocket_context) as websocket:
            await websocket.send("encrypted websocket message")
            assert await websocket.recv() == "encrypted websocket message"
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def test_shutdown_cancels_clients_that_never_identify_a_protocol(monkeypatch, tmp_path):
    asyncio.run(_exercise_idle_shutdown(monkeypatch, tmp_path))


async def _exercise_idle_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setattr(BaseConfig, "SSL_KEYFILE_PASSWORD", "")
    cert, key = ensure_local_tls(str(tmp_path), ["localhost"])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = _mk_hypercorn_config("127.0.0.1", port, cert, key)
    config.graceful_timeout = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve_with_http_recovery(Starlette(), config, shutdown_trigger=stop.wait)
    )
    client_reader = client_writer = None
    try:
        for _ in range(100):
            try:
                client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.01)
        else:
            raise AssertionError("TLS listener never became ready")
        stop.set()
        await asyncio.wait_for(task, 2)
        assert await asyncio.wait_for(client_reader.read(), 1) == b""
    finally:
        stop.set()
        if client_writer is not None:
            client_writer.close()
            await client_writer.wait_closed()
        if not task.done():
            await asyncio.wait_for(task, 2)


def test_private_listener_rejects_unmapped_clients_even_with_forged_forwarding_headers():
    async def endpoint(request):
        pytest.fail("An unmapped private peer reached the application")
    client = TestClient(PrivateRelayApp(Starlette(routes=[Route("/api/check", endpoint)]), {}))
    response = client.get("/api/check", headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"})
    assert response.status_code == 403


def test_private_relay_restores_remote_identity_without_trusting_headers():
    async def endpoint(request):
        return JSONResponse({"client": request.client.host, "port": request.scope["server"][1]})
    peers = {("testclient", 50000): (("203.0.113.10", 54321), ("192.0.2.10", 1515), True)}
    client = TestClient(PrivateRelayApp(Starlette(routes=[Route("/api/check", endpoint)]), peers))
    result = client.get("/api/check", headers={"X-Forwarded-For": "127.0.0.1"})
    assert result.json() == {"client": "203.0.113.10", "port": 1515}
