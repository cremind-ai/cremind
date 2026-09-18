"""One ``POST /api/config/setup`` per profile at a time.

Setup used to hold the event loop for its whole duration, which serialised
these calls by accident. Now that the blocking spans run on worker threads a
second POST really can interleave with the first — and the most likely second
caller is the user's own retry after a dropped connection, since the server
finishes the run whether or not anyone is still listening.

Two invariants would break without the guard. The handler's "profile already
exists" check happens long before the writes, so two calls for the same name
both pass it, interleave their config, and each mint a token. And
``SetupProgressBus`` is process-global with no run key — its docstring says
"Only one setup runs at a time" — so two wizards would interleave log lines
into each other's panels.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api


class _FakeConfigStorage:
    def is_setup_complete(self) -> bool:
        return True

    def get(self, table: str, key: str, **_kw: Any) -> str | None:
        if (table, key) == ("server_config", "jwt_secret"):
            return "test-jwt-secret-long-enough-for-hs256"
        return None

    def set(self, *_a: Any, **_kw: Any) -> None:
        return None


class _GatedConversationStorage:
    """Conversation storage that parks inside the handler until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.created: list[str] = []

    async def profile_exists(self, _name: str) -> bool:
        return False

    async def create_profile(self, name: str) -> None:
        self.entered.set()
        await self.release.wait()
        self.created.append(name)


def _handler(conversation_storage) -> Callable:
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=_FakeConfigStorage(),
        conversation_storage=conversation_storage,
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
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)
    monkeypatch.setattr(config_api, "_features_required_by_setup_payload", lambda _b: [])
    monkeypatch.setattr(config_api, "ensure_persona_file", lambda _p: None)
    monkeypatch.setattr("app.auth.write_token_file", lambda _p, _t: str(tmp_path / "t"))
    monkeypatch.setattr(config_api, "_generate_token", lambda _s, _p, hours=None: ("t", "later"))
    # The guard is module-level state; a leaked entry would fail the next test.
    yield
    config_api._setup_in_flight.clear()


def test_a_second_call_for_the_same_profile_is_refused(setup_env) -> None:
    conversations = _GatedConversationStorage()
    handler = _handler(conversations)

    async def scenario():
        first = asyncio.create_task(handler(_request({"profile": "javis"})))
        await conversations.entered.wait()  # first call is mid-flight

        second = await handler(_request({"profile": "javis"}))

        conversations.release.set()
        return second, await first

    second, first = asyncio.run(scenario())

    assert second.status_code == 409, second.body
    assert json.loads(second.body)["code"] == "setup_in_progress"
    assert first.status_code == 200, first.body
    # Exactly one run did the work.
    assert conversations.created == ["javis"]


def test_a_different_profile_is_not_blocked(setup_env) -> None:
    """The guard is per profile — onboarding two names at once is legitimate."""
    conversations = _GatedConversationStorage()
    handler = _handler(conversations)

    async def scenario():
        first = asyncio.create_task(handler(_request({"profile": "javis"})))
        await conversations.entered.wait()
        conversations.release.set()
        other = await handler(_request({"profile": "bobo"}))
        return other, await first

    other, first = asyncio.run(scenario())

    assert first.status_code == 200, first.body
    assert other.status_code == 200, other.body


def test_the_slot_is_released_so_a_retry_can_proceed(setup_env) -> None:
    """A finished run must not leave the name permanently locked."""
    conversations = _GatedConversationStorage()
    conversations.release.set()
    handler = _handler(conversations)

    first = asyncio.run(handler(_request({"profile": "javis"})))
    assert first.status_code == 200, first.body
    assert "javis" not in config_api._setup_in_flight

    second = asyncio.run(handler(_request({"profile": "javis"})))
    # Not a 409 from the guard — the run is over. (It answers on its own merits;
    # here the fake storage still reports the profile absent, so it succeeds.)
    assert second.status_code == 200, second.body


def test_the_slot_is_released_when_setup_fails(setup_env) -> None:
    """An error path must release too, or one bad attempt bricks the name."""

    class _Exploding(_GatedConversationStorage):
        async def create_profile(self, _name: str) -> None:
            raise RuntimeError("boom")

    handler = _handler(_Exploding())
    with pytest.raises(RuntimeError):
        asyncio.run(handler(_request({"profile": "javis"})))

    assert "javis" not in config_api._setup_in_flight
