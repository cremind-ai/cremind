"""Auth regression for GET/PUT /api/config/server.

These two handlers shipped with no authorization check at all. Their docstrings
claimed one ("Requires auth", "admin only") and every caller sent an admin
token, so nothing ever noticed — but the JWT middleware only *populates*
``request.user``, it rejects nobody, so any anonymous caller could read the
server's non-secret configuration (vector-store hosts, Chroma paths, the user
working directory) and, worse, PUT to it.

It was found while giving the configuration-file export a gate of its own: that
endpoint is deliberately open to every profile, so its neighbours had to be
checked rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

from app.api import config as config_api


class _FakeConfigStorage:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_all(self, section: str, include_secrets: bool = False) -> dict[str, str]:
        return {k[1]: v for k, v in self.values.items() if k[0] == section}

    def set(self, section: str, key: str, value: str, is_secret: bool = False) -> None:
        self.values[(section, key)] = value

    def is_setup_complete(self) -> bool:
        return True


def _handler(state, method: str) -> Callable:
    for route in config_api.get_config_routes(state):  # type: ignore[arg-type]
        if route.path == "/api/config/server" and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} /api/config/server route not registered")


def _state() -> tuple[SimpleNamespace, _FakeConfigStorage]:
    storage = _FakeConfigStorage()
    return SimpleNamespace(storage_ready=True, config_storage=storage), storage


def _req(*, authenticated: bool = True, username: str = "admin", body: dict | None = None):
    async def _json():
        return body or {}

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authenticated, username=username),
        json=_json,
    )


def test_get_server_config_rejects_anonymous_caller() -> None:
    state, _ = _state()
    resp = asyncio.run(_handler(state, "GET")(_req(authenticated=False, username="")))
    assert resp.status_code == 401, resp.body


def test_get_server_config_rejects_non_admin_profile() -> None:
    """A tenant's own settings live on /api/config/user; this table is the
    server's, and naming its vector-store hosts and paths is deployment
    detail — the same line /api/system/environment draws."""
    state, _ = _state()
    resp = asyncio.run(_handler(state, "GET")(_req(username="alice")))
    assert resp.status_code == 403, resp.body
    assert json.loads(resp.body)["error"] == "Admin profile required"


def test_get_server_config_allows_admin() -> None:
    state, storage = _state()
    storage.set("server_config", "user_working_dir", "/srv/cremind")
    resp = asyncio.run(_handler(state, "GET")(_req(username="admin")))
    assert resp.status_code == 200, resp.body
    assert json.loads(resp.body)["config"]["user_working_dir"] == "/srv/cremind"


def test_put_server_config_rejects_non_admin_and_writes_nothing() -> None:
    state, storage = _state()
    resp = asyncio.run(
        _handler(state, "PUT")(_req(username="alice", body={"config": {"log_level": "debug"}})),
    )
    assert resp.status_code == 403, resp.body
    assert storage.values == {}


def test_put_server_config_allows_admin_and_still_drops_bootstrap_only_keys() -> None:
    """The gate must not disturb the existing rule: db_provider, postgres and
    system_dir live in bootstrap.toml and are silently ignored here."""
    state, storage = _state()
    resp = asyncio.run(_handler(state, "PUT")(_req(
        username="admin",
        body={"config": {"log_level": "debug", "db_provider": "postgres", "system_dir": "/x"}},
    )))
    assert resp.status_code == 200, resp.body
    assert storage.values == {("server_config", "log_level"): "debug"}


def test_auth_is_checked_before_storage_readiness() -> None:
    """An anonymous caller hears 401, not a 503 that would confirm the server
    exists but has never been set up."""
    state, _ = _state()
    state.storage_ready = False
    resp = asyncio.run(_handler(state, "GET")(_req(authenticated=False, username="")))
    assert resp.status_code == 401, resp.body
