"""``POST /api/config/setup`` must not starve the event loop.

The handler is ``async def``, but its body used to call a long chain of
*synchronous* work directly on the loop: the pip installer, and one blocking DB
round-trip per config key for ~30 built-in tools. On a real Postgres install
that was a ~52-second window in which the server answered nothing at all.

On a plain HTTP/1.1 install that is merely a slow request. On the Kubernetes
HTTPS deployment it is fatal: the TLS listener advertises ``h2`` via ALPN, so
every request from a browser origin rides **one** multiplexed connection. A
loop that goes silent for a minute takes that whole connection down with it —
which is why the setup POST, ``/version`` and ``/api/tls/status`` all failed
together while the server went on to create the profile.

So the property under test is not "setup is fast". It is "other requests are
still served while setup runs".
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api

# Enough blocking work to be unmistakable, but still a fraction of a second.
_WRITE_COUNT = 20
_WRITE_SLEEP = 0.02  # 20 writes * 20ms = ~400ms of blocking work
_HEARTBEAT_INTERVAL = 0.005

# Deliberately far below the ~80 ticks a free loop manages, so the test pins the
# difference between "serving" and "frozen" without being timing-flaky in CI.
_MIN_TICKS = 5


class _SlowConfigStorage:
    """Config storage whose every write costs a blocking round-trip."""

    def __init__(self) -> None:
        self.writes = 0

    def is_setup_complete(self) -> bool:
        return True  # non-first setup: the path a second profile takes

    def get(self, table: str, key: str, **_kw: Any) -> str | None:
        if (table, key) == ("server_config", "jwt_secret"):
            return "test-jwt-secret-long-enough-for-hs256"
        return None

    def set(self, _table: str, _key: str, _value: str, **_kw: Any) -> None:
        self.writes += 1
        time.sleep(_WRITE_SLEEP)


class _FakeConversationStorage:
    async def profile_exists(self, _name: str) -> bool:
        return False

    async def create_profile(self, _name: str) -> None:
        return None


def _handler(config_storage) -> Callable:
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=config_storage,
        conversation_storage=_FakeConversationStorage(),
        registry=None,
        on_first_setup=None,
        boot_fn=None,
    )
    for route in config_api.get_config_routes(state):  # type: ignore[arg-type]
        if route.path == "/api/config/setup" and "POST" in route.methods:
            return route.endpoint
    raise AssertionError("POST /api/config/setup route not registered")


def _request(body: dict[str, Any]):
    async def _json() -> dict:
        return body

    return SimpleNamespace(headers={"host": "localhost:1515"}, cookies={}, json=_json)


@pytest.fixture
def setup_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # Setup creates the profile's working directory; keep it out of the real one.
    monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)
    monkeypatch.setattr(config_api, "_features_required_by_setup_payload", lambda _b: [])
    monkeypatch.setattr(config_api, "ensure_persona_file", lambda _p: None)
    monkeypatch.setattr("app.auth.write_token_file", lambda _p, _t: str(tmp_path / "t"))
    # Minting reads the profile's revocation serial from the real DB, which
    # would build an engine against the developer's own ~/.cremind install.
    # Nothing here is about token shape, so keep it off the disk entirely.
    monkeypatch.setattr(config_api, "_generate_token", lambda _s, _p, hours=None: ("t", "later"))


def test_other_requests_are_served_while_setup_runs(setup_env) -> None:
    storage = _SlowConfigStorage()
    body = {
        "profile": "javis",
        # Each key is one blocking write inside the config-write span.
        "llm_config": {f"provider{i}.api_key": f"k{i}" for i in range(_WRITE_COUNT)},
    }

    async def scenario() -> tuple[int, Any]:
        ticks = 0

        async def heartbeat() -> None:
            """Stands in for the /version and /api/tls/status polls."""
            nonlocal ticks
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            response = await _handler(storage)(_request(body))
        finally:
            beat.cancel()
        return ticks, response

    ticks, response = asyncio.run(scenario())

    assert response.status_code == 200, response.body
    # Sanity: the blocking work really did run, so a passing tick count means
    # the loop stayed free rather than the writes being skipped.
    assert storage.writes >= _WRITE_COUNT
    assert ticks >= _MIN_TICKS, (
        f"the event loop was starved during setup: only {ticks} heartbeat tick(s) "
        f"landed while {storage.writes} blocking writes ran"
    )
