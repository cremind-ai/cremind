"""Tests for app.server's shutdown machinery.

Two halves, both about a shutdown that must always complete:

``_on_shutdown`` is registered as Starlette ``on_shutdown`` and runs inside
the ASGI lifespan dispatcher. If its inner cleanup body hangs (e.g.,
a channel adapter's ``stop()`` blocks on a hung network call),
uvicorn's ``timeout_graceful_shutdown`` does NOT save us — that only
bounds connection drain. The hook MUST self-bound. These tests verify
it does, so a Docker container restart triggered by the in-app
upgrade flow always proceeds.

``request_graceful_shutdown`` is the seam that lets a restart run that same
cleanup at all. Before it existed, the restart was a signal from a detached
helper — which on Windows is ``TerminateProcess``, i.e. the cleanup above
never ran and every sidecar the server had spawned was orphaned.

The tests drive the coroutines via ``asyncio.run`` directly to avoid
depending on pytest-asyncio (which the project doesn't pin).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from app import server
from app.system.boot_service import RESTART_DELIBERATE_FILE


@pytest.fixture(autouse=True)
def _isolate_system_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep any marker write inside the test's own tmp dir."""
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _seam_is_clean():
    """No test may leak a seam into the next one."""
    server._clear_shutdown_seam()
    yield
    server._clear_shutdown_seam()


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


# ── request_graceful_shutdown ────────────────────────────────────────────


def _supervise(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    for name in ("INSTALL_MODE", "CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED"):
        monkeypatch.delenv(name, raising=False)
    if on:
        monkeypatch.setenv("CREMIND_SUPERVISED", "1")


def test_shutdown_request_without_a_seam_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is serving, so nothing can be asked to stop — and the caller
    has to be told, because its watchdog is the only thing left."""
    _supervise(monkeypatch, on=True)

    assert server.request_graceful_shutdown(0.0) is False
    # No marker either: a stop that never starts must not excuse a later crash.
    assert not (tmp_path / RESTART_DELIBERATE_FILE).exists()


def test_shutdown_request_schedules_the_initiator_after_the_delay() -> None:
    """The delay is the response window — the 202 has to reach the client
    before the listener starts going away."""
    fired: list[float] = []

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        server._install_shutdown_seam(loop, lambda: fired.append(loop.time() - started))
        assert server.request_graceful_shutdown(0.15) is True
        assert fired == [], "must not fire synchronously"
        await asyncio.sleep(0.4)

    asyncio.run(_run())
    assert len(fired) == 1
    assert fired[0] >= 0.15


def test_shutdown_request_works_from_another_thread() -> None:
    """The sentinel watcher runs in its own thread; touching the loop from
    there without call_soon_threadsafe would be a data race."""
    fired: list[bool] = []

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        server._install_shutdown_seam(loop, lambda: fired.append(True))
        thread = threading.Thread(
            target=lambda: server.request_graceful_shutdown(0.0)
        )
        thread.start()
        thread.join(timeout=2)
        await asyncio.sleep(0.3)

    asyncio.run(_run())
    assert fired == [True]


def test_the_deliberate_marker_is_written_only_when_supervised(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows respawn loop reads it to tell a restart from a crash
    loop. Nothing reads it when no supervisor exists."""
    marker = tmp_path / RESTART_DELIBERATE_FILE

    _supervise(monkeypatch, on=False)
    server._write_deliberate_restart_marker()
    assert not marker.exists()

    _supervise(monkeypatch, on=True)
    server._write_deliberate_restart_marker()
    assert marker.is_file()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert isinstance(payload["requested_at"], float)


def test_the_marker_lands_even_if_the_shutdown_never_runs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is written when the request is made, not when the callback runs —
    a wedged loop is exactly when the supervisor most needs to know the stop
    was deliberate."""
    _supervise(monkeypatch, on=True)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        # An initiator that never gets a chance to run: 10s from now.
        server._install_shutdown_seam(loop, lambda: None)
        assert server.request_graceful_shutdown(10.0) is True

    asyncio.run(_run())
    assert (tmp_path / RESTART_DELIBERATE_FILE).is_file()


def test_clearing_the_seam_retracts_the_trigger() -> None:
    async def _run() -> bool:
        server._install_shutdown_seam(asyncio.get_running_loop(), lambda: None)
        server._clear_shutdown_seam()
        return server.request_graceful_shutdown(0.0)

    assert asyncio.run(_run()) is False
    assert server._shutdown_loop is None
    assert server._initiate_shutdown is None
