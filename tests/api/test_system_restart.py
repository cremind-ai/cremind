"""Tests for POST /api/system/restart.

The endpoint's job is an ordering, not a kill: spawn the watchdog, and only
then ask this process to shut itself down. Both halves matter. Skipping the
watchdog leaves a wedged shutdown with nothing to rescue it; skipping the
in-process request is what the old design did, and on Windows that meant the
detached helper's ``os.kill`` — ``TerminateProcess`` — so the lifespan cleanup
never ran and channel sidecars were orphaned.

The Popen call itself is mocked: a cross-platform detach invocation is trusted
at the OS layer (same posture as tests/upgrade/test_api.py).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.testclient import TestClient

from app import server as app_server
from app.api.system import _RESPONSE_WINDOW_S, get_system_routes
from app.system.restart import DEFAULT_GRACE_S


# ── auth helpers ─────────────────────────────────────────────────────────


class _AlwaysAdmin(AuthenticationBackend):
    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), SimpleUser("admin")


class _AlwaysAnon(AuthenticationBackend):
    async def authenticate(self, conn):
        return AuthCredentials([]), None


class _AlwaysBob(AuthenticationBackend):
    """Authenticated, but not the admin profile."""

    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), SimpleUser("bob")


def _make_app(backend: AuthenticationBackend) -> Starlette:
    return Starlette(
        routes=get_system_routes(),
        middleware=[Middleware(AuthenticationMiddleware, backend=backend)],
    )


@pytest.fixture(autouse=True)
def _isolate_system_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))


# ── auth gating ──────────────────────────────────────────────────────────


def test_restart_rejects_unauth() -> None:
    client = TestClient(_make_app(_AlwaysAnon()))
    r = client.post("/api/system/restart")
    assert r.status_code == 401


def test_restart_rejects_a_non_admin_profile() -> None:
    client = TestClient(_make_app(_AlwaysBob()))
    r = client.post("/api/system/restart")
    assert r.status_code == 403


# ── the happy path ───────────────────────────────────────────────────────


def test_restart_spawns_the_watchdog_then_asks_itself_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[Any] = []
    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        order.append("spawn")
        captured["cmd"] = cmd
        return MagicMock(pid=4242)

    def fake_request(delay_s: float) -> bool:
        order.append(("shutdown", delay_s))
        return True

    import app.api.system as system_api

    monkeypatch.setattr(system_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(app_server, "request_graceful_shutdown", fake_request)

    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.post("/api/system/restart")

    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["pid"] == 4242
    assert body["status"] == "restarting"

    # The watchdog exists before the process starts dying, never after.
    assert order == ["spawn", ("shutdown", _RESPONSE_WINDOW_S)]

    cmd = captured["cmd"]
    assert cmd[1:4] == ["-m", "app.system.restart", "--parent-pid"]
    assert cmd[cmd.index("--grace") + 1] == str(DEFAULT_GRACE_S)


def test_a_failed_spawn_leaves_the_server_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a watchdog there is no backstop, so the shutdown must not be
    requested at all — better a server that is still up than one that stops
    with nothing able to finish the job."""
    requested: list[float] = []

    def boom(cmd, **kwargs):
        raise OSError("no fork for you")

    import app.api.system as system_api

    monkeypatch.setattr(system_api.subprocess, "Popen", boom)
    monkeypatch.setattr(
        app_server,
        "request_graceful_shutdown",
        lambda delay_s: requested.append(delay_s) or True,
    )

    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.post("/api/system/restart")

    assert r.status_code == 500
    assert "Failed to spawn restart helper" in r.json()["error"]
    assert requested == []


def test_restart_still_returns_202_when_nothing_is_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No seam (a test client, a teardown window) is not a client error: the
    watchdog is spawned and will stop the process at its deadline."""
    warnings: list[str] = []

    import app.api.system as system_api

    monkeypatch.setattr(
        system_api.subprocess, "Popen", lambda cmd, **kw: MagicMock(pid=7)
    )
    monkeypatch.setattr(app_server, "request_graceful_shutdown", lambda delay_s: False)
    monkeypatch.setattr(system_api.logger, "warning", lambda msg: warnings.append(msg))

    client = TestClient(_make_app(_AlwaysAdmin()))
    r = client.post("/api/system/restart")

    assert r.status_code == 202
    assert warnings and "no serving loop" in warnings[0]
