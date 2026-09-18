"""Auth gating on ``GET /api/config/setup/stream``.

The stream is open during the first-run bootstrap window — no JWT can exist
yet — and admin-only once setup is complete, so a running wizard cannot leak
progress to anonymous clients afterwards.

The post-setup half of that gate had no coverage, and the web wizard sent no
``Authorization`` header at all, so **every** profile created after the first
got a 401 here. The call site passed no error handler, so the failure was
invisible: the live-log panel simply stayed empty for the whole run, which is
indistinguishable from the server hanging. That is what these tests pin.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import setup_stream as setup_stream_api


def _stream_handler() -> Callable:
    for route in setup_stream_api.get_setup_stream_routes():
        if route.path == "/api/config/setup/stream" and "GET" in route.methods:
            return route.endpoint
    raise AssertionError("GET /api/config/setup/stream route not registered")


def _request(*, authenticated: bool, username: str = "") -> SimpleNamespace:
    user = SimpleNamespace(is_authenticated=authenticated, username=username)
    return SimpleNamespace(headers={}, user=user)


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch):
    """Drive ``get_state`` so each test picks its own setup phase."""
    holder = SimpleNamespace(storage_ready=True, config_storage=None)
    monkeypatch.setattr(setup_stream_api, "get_state", lambda: holder)
    return holder


def _set_phase(state, *, complete: bool, storage_ready: bool = True) -> None:
    state.storage_ready = storage_ready
    state.config_storage = SimpleNamespace(is_setup_complete=lambda: complete)


def _call(handler: Callable, request: Any):
    return asyncio.run(handler(request))


def test_the_bootstrap_window_is_open_to_anonymous_clients(state) -> None:
    """First-run setup has no JWT to present, so the gate must not demand one."""
    _set_phase(state, complete=False)
    response = _call(_stream_handler(), _request(authenticated=False))
    assert response.media_type == "text/event-stream"


def test_deferred_storage_is_also_open(state) -> None:
    """Before ``bootstrap.toml`` exists there is no DB to ask, let alone a token."""
    state.storage_ready = False
    state.config_storage = None
    response = _call(_stream_handler(), _request(authenticated=False))
    assert response.media_type == "text/event-stream"


def test_post_setup_an_anonymous_client_is_refused(state) -> None:
    """The regression: this is what the wizard hit on every profile after the first."""
    _set_phase(state, complete=True)
    response = _call(_stream_handler(), _request(authenticated=False))
    assert response.status_code == 401


def test_post_setup_a_non_admin_is_refused(state) -> None:
    _set_phase(state, complete=True)
    response = _call(_stream_handler(), _request(authenticated=True, username="javis"))
    assert response.status_code == 403


def test_post_setup_admin_gets_the_stream(state) -> None:
    """The case the web wizard now satisfies by sending the admin token."""
    _set_phase(state, complete=True)
    response = _call(_stream_handler(), _request(authenticated=True, username="admin"))
    assert response.media_type == "text/event-stream"
