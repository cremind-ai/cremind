"""Tests for the CLI-side Codex OAuth callback listener (app/cli/codex_listener.py).

This is what makes `cremind llm codex-oauth login` work against a remote or
containerized server: the redirect URI is pinned to localhost:1455, so the CLI
binds that port on the *client* machine and relays the code. Every test drives a
real ephemeral socket — the parsing and the HTTP framing are the whole point.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from app.cli.codex_listener import CallbackResult, LocalCallbackListener


STATE = "abcdef0123456789abcdef"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _http_get(port: int, target: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=5.0)
    writer.close()
    return data.decode("utf-8", "replace")


def test_captures_code_and_builds_redirect_url():
    port = _free_port()

    async def _run():
        listener = LocalCallbackListener(STATE, port=port)
        assert await listener.start() is True
        try:
            waiter = asyncio.ensure_future(listener.wait())
            page = await _http_get(port, f"/auth/callback?code=THECODE&state={STATE}")
            assert "Signed in to ChatGPT" in page
            result = await asyncio.wait_for(waiter, timeout=5.0)
            assert result.error == ""
            # The server is handed the canonical URL, matching what the user
            # would otherwise have pasted from the address bar.
            assert result.redirect_url == (
                f"http://localhost:{port}/auth/callback?code=THECODE&state={STATE}"
            )
        finally:
            listener.close()

    asyncio.run(_run())


def test_captures_provider_error():
    port = _free_port()

    async def _run():
        listener = LocalCallbackListener(STATE, port=port)
        assert await listener.start() is True
        try:
            waiter = asyncio.ensure_future(listener.wait())
            page = await _http_get(
                port,
                f"/auth/callback?error=access_denied&error_description=User+declined&state={STATE}",
            )
            assert "Sign-in failed" in page
            result = await asyncio.wait_for(waiter, timeout=5.0)
            assert result.redirect_url == ""
            assert result.error == "User declined"
        finally:
            listener.close()

    asyncio.run(_run())


def test_alias_path_is_accepted():
    port = _free_port()

    async def _run():
        listener = LocalCallbackListener(STATE, port=port)
        assert await listener.start() is True
        try:
            waiter = asyncio.ensure_future(listener.wait())
            await _http_get(port, f"/callback?code=C&state={STATE}")
            result = await asyncio.wait_for(waiter, timeout=5.0)
            assert "/callback?code=C" in result.redirect_url
        finally:
            listener.close()

    asyncio.run(_run())


def test_foreign_state_does_not_resolve_or_abort():
    """A stale tab from an earlier attempt must not hijack or kill this flow."""
    port = _free_port()

    async def _run():
        listener = LocalCallbackListener(STATE, port=port)
        assert await listener.start() is True
        try:
            waiter = asyncio.ensure_future(listener.wait())
            page = await _http_get(port, "/auth/callback?code=C&state=someotherstate")
            assert "Sign-in failed" in page
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), timeout=0.3)
            # Still live: the real callback lands afterwards and wins.
            await _http_get(port, f"/auth/callback?code=REAL&state={STATE}")
            result = await asyncio.wait_for(waiter, timeout=5.0)
            assert "code=REAL" in result.redirect_url
        finally:
            listener.close()

    asyncio.run(_run())


def test_unrelated_path_404s_without_resolving():
    port = _free_port()

    async def _run():
        listener = LocalCallbackListener(STATE, port=port)
        assert await listener.start() is True
        try:
            resp = await _http_get(port, "/nope")
            assert "404" in resp.split("\r\n", 1)[0]
            resp = await _http_get(port, "/auth/callback")  # no code, no state
            assert "Sign-in failed" in resp
        finally:
            listener.close()

    asyncio.run(_run())


def test_start_returns_false_when_port_is_taken():
    """The native-install case: the server already owns 1455, so the CLI simply
    leaves capture to it rather than treating the clash as an error."""
    port = _free_port()

    async def _run():
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
        try:
            listener = LocalCallbackListener(STATE, port=port)
            assert await listener.start() is False
            listener.close()
        finally:
            blocker.close()
            await blocker.wait_closed()

    asyncio.run(_run())


def test_close_releases_the_port():
    port = _free_port()

    async def _run():
        first = LocalCallbackListener(STATE, port=port)
        assert await first.start() is True
        first.close()
        # Give the loop a tick to actually tear the socket down.
        await asyncio.sleep(0.1)
        second = LocalCallbackListener(STATE, port=port)
        assert await second.start() is True
        second.close()

    asyncio.run(_run())


def test_wait_before_start_is_a_programming_error():
    async def _run():
        with pytest.raises(RuntimeError):
            await LocalCallbackListener(STATE, port=_free_port()).wait()

    asyncio.run(_run())


def test_post_is_rejected():
    port = _free_port()

    async def _run():
        listener = LocalCallbackListener(STATE, port=port)
        assert await listener.start() is True
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"POST /auth/callback?code=C&state={STATE} HTTP/1.1\r\n"
                "Host: localhost\r\nContent-Length: 0\r\n\r\n".encode()
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(65536), timeout=5.0)
            writer.close()
            assert "405" in data.decode("utf-8", "replace").split("\r\n", 1)[0]
        finally:
            listener.close()

    asyncio.run(_run())


def test_callback_result_defaults():
    assert CallbackResult().redirect_url == ""
    assert CallbackResult().error == ""
