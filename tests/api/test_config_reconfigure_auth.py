"""Auth regression for POST /api/config/reconfigure.

The handler clears ``server_config.setup_complete``, which re-opens the
unauthenticated first-setup branch of POST /api/config/setup — the branch that
mints a fresh admin JWT. So an unauthenticated reconfigure is a full
privilege-escalation path, not just an annoying state reset, and the whole
profile-authorization story rests on this one gate.

Everything user-facing already promised the gate (the handler docstring, the
CLI client wrapper, ``cremind setup`` help, the bundled CLI doc, and the
error text of the unauthenticated ``reset-orphaned-setup`` sibling), so these
tests pin the promise to the implementation.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

from app.api import config as config_api


class _FakeConfigStorage:
    """Just enough config storage to observe the setup_complete delete."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {("server_config", "setup_complete"): "true"}

    def delete(self, section: str, key: str) -> None:
        self.values.pop((section, key), None)

    def is_setup_complete(self) -> bool:
        return ("server_config", "setup_complete") in self.values


def _handler(state) -> Callable:
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    for route in routes:
        if route.path == "/api/config/reconfigure" and "POST" in route.methods:
            return route.endpoint
    raise AssertionError("POST /api/config/reconfigure route not registered")


def _state() -> tuple[SimpleNamespace, _FakeConfigStorage]:
    storage = _FakeConfigStorage()
    return (
        SimpleNamespace(
            storage_ready=True,
            config_storage=storage,
            conversation_storage=SimpleNamespace(),
        ),
        storage,
    )


def _req(*, authenticated: bool = True, username: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(is_authenticated=authenticated, username=username))


def test_reconfigure_rejects_anonymous_caller() -> None:
    """No token at all → 401, and setup_complete must survive untouched."""
    state, storage = _state()
    resp = asyncio.run(_handler(state)(_req(authenticated=False, username="")))
    assert resp.status_code == 401, resp.body
    assert storage.is_setup_complete()


def test_reconfigure_rejects_non_admin_profile() -> None:
    """A valid token for an ordinary profile → 403. Reconfigure is a
    server-wide reset; a tenant must not be able to trigger it."""
    state, storage = _state()
    resp = asyncio.run(_handler(state)(_req(username="alice")))
    assert resp.status_code == 403, resp.body
    assert json.loads(resp.body)["error"] == "Admin profile required"
    assert storage.is_setup_complete()


def test_reconfigure_allows_admin_and_clears_setup_complete() -> None:
    """The gate must not break the one caller that is supposed to work —
    the admin's Settings → Profiles → Reconfigure button and
    ``cremind setup reconfigure``."""
    state, storage = _state()
    resp = asyncio.run(_handler(state)(_req(username="admin")))
    assert resp.status_code == 200, resp.body
    assert json.loads(resp.body)["success"] is True
    assert not storage.is_setup_complete()


def test_reconfigure_checks_auth_before_storage_readiness() -> None:
    """Auth runs first: in deferred-storage mode an anonymous caller gets 401,
    not the 503 that would leak whether the DB is up yet."""
    state, _storage = _state()
    state.storage_ready = False
    resp = asyncio.run(_handler(state)(_req(authenticated=False, username="")))
    assert resp.status_code == 401, resp.body
