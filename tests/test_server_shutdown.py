"""Tests for app.server._on_shutdown's bounded-shutdown behaviour.

The hook is registered as Starlette ``on_shutdown`` and runs inside
the ASGI lifespan dispatcher. If its inner cleanup body hangs (e.g.,
a channel adapter's ``stop()`` blocks on a hung network call),
uvicorn's ``timeout_graceful_shutdown`` does NOT save us — that only
bounds connection drain. The hook MUST self-bound. These tests verify
it does, so a Docker container restart triggered by the in-app
upgrade flow always proceeds.

The tests drive the coroutines via ``asyncio.run`` directly to avoid
depending on pytest-asyncio (which the project doesn't pin).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from app import server


def test_on_shutdown_returns_within_grace_when_inner_hangs() -> None:
    """If _do_shutdown stalls, _on_shutdown returns within SHUTDOWN_TIMEOUT_S
    plus a tiny scheduler slack — not 60s, not forever."""

    async def _hang() -> None:
        await asyncio.sleep(60)

    async def _run() -> float:
        with patch.object(server, "_do_shutdown", _hang):
            start = time.monotonic()
            await server._on_shutdown()
            return time.monotonic() - start

    elapsed = asyncio.run(_run())

    # The wrapper is asyncio.wait_for(..., timeout=SHUTDOWN_TIMEOUT_S). Allow
    # a small grace for scheduler overhead, but it must be FAR less than
    # the inner sleep would have taken.
    assert elapsed >= server.SHUTDOWN_TIMEOUT_S - 0.5
    assert elapsed < server.SHUTDOWN_TIMEOUT_S + 2.0


def test_on_shutdown_returns_promptly_when_inner_completes() -> None:
    """The bound is a ceiling, not a floor. Fast cleanups should not be delayed."""

    async def _fast() -> None:
        await asyncio.sleep(0)

    async def _run() -> float:
        with patch.object(server, "_do_shutdown", _fast):
            start = time.monotonic()
            await server._on_shutdown()
            return time.monotonic() - start

    elapsed = asyncio.run(_run())
    assert elapsed < 0.5


def test_on_shutdown_logs_warning_on_timeout() -> None:
    """When the timeout fires, a warning is logged so operators can see why
    cleanup was abandoned."""

    async def _hang() -> None:
        await asyncio.sleep(60)

    async def _run(warn_mock) -> None:
        with patch.object(server, "_do_shutdown", _hang):
            with patch.object(server.logger, "warning", warn_mock):
                await server._on_shutdown()

    from unittest.mock import MagicMock

    warn = MagicMock()
    asyncio.run(_run(warn))
    assert warn.called
    msg = warn.call_args[0][0]
    assert "exceeded" in msg
    assert "abandoning" in msg
