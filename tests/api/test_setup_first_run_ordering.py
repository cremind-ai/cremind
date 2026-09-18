"""First-run setup must not close the bootstrap window before minting a token.

``mark_setup_complete()`` used to run right after the embedding config was
persisted — roughly 200 lines before the admin token was generated and written,
and well after ``create_profile``. Anything that killed the process in between
(a pod restart, an OOM kill, an unhandled exception) left the install in a state
with no way out:

- ``setup_complete`` is true, so ``POST /api/config/setup`` takes the
  non-first-setup branch and demands an admin JWT,
- the admin profile exists but no token was ever written, so there is no JWT to
  present — every retry is a 401,
- and ``POST /api/config/reset-orphaned-setup`` refuses, because it only fires
  when *no* visible profile exists.

The only escape was wiping the database. Ordering the flag after
``write_token_file`` turns that dead end into a plain retry.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api


class _RecordingConfigStorage:
    """Records the order of the calls this test is about."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._complete = False

    def is_setup_complete(self) -> bool:
        return self._complete

    def mark_setup_complete(self) -> None:
        self._complete = True
        self.events.append("mark_setup_complete")

    def get(self, table: str, key: str, **_kw: Any) -> str | None:
        if (table, key) == ("server_config", "jwt_secret"):
            return "test-jwt-secret-long-enough-for-hs256"
        return None

    def set(self, *_a: Any, **_kw: Any) -> None:
        return None


class _FakeConversationStorage:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def profile_exists(self, name: str) -> bool:
        return name in self.created

    async def create_profile(self, name: str) -> None:
        self.created.append(name)


def _handler(config_storage, conversation_storage) -> Callable:
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=config_storage,
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
    monkeypatch.setattr(config_api, "_features_required_by_setup_payload", lambda _b: [])
    monkeypatch.setattr(config_api, "ensure_persona_file", lambda _p: None)
    monkeypatch.setattr(config_api, "_generate_token", lambda _s, _p, hours=None: ("t", "later"))
    monkeypatch.setattr(config_api, "_resolve_vectorstore", _noop_vectorstore)
    monkeypatch.setattr("app.lib.embedding_lifecycle.persist_embedding_config", lambda *_a: None)
    yield
    config_api._setup_in_flight.clear()


async def _noop_vectorstore(_config: dict) -> dict:
    return {}


def test_setup_complete_is_marked_only_after_the_token_is_on_disk(
    setup_env, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    storage = _RecordingConfigStorage()

    def _write_token_file(profile: str, _token: str) -> str:
        storage.events.append("write_token_file")
        return str(tmp_path / f"{profile}.token")

    monkeypatch.setattr("app.auth.write_token_file", _write_token_file)

    response = asyncio.run(
        _handler(storage, _FakeConversationStorage())(_request({"profile": "admin"}))
    )
    assert response.status_code == 200, response.body

    assert storage.events == ["write_token_file", "mark_setup_complete"], (
        "the bootstrap window must stay open until a usable credential exists"
    )


def test_a_crash_before_the_token_leaves_setup_retryable(
    setup_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The brick scenario: die while minting, then try again."""
    storage = _RecordingConfigStorage()
    conversations = _FakeConversationStorage()

    def _explode(_profile: str, _token: str) -> str:
        raise RuntimeError("pod restarted mid-setup")

    monkeypatch.setattr("app.auth.write_token_file", _explode)
    with pytest.raises(RuntimeError):
        asyncio.run(_handler(storage, conversations)(_request({"profile": "admin"})))

    # The profile was created, but the window is still open...
    assert conversations.created == ["admin"]
    assert storage.is_setup_complete() is False, (
        "marking setup complete before the token exists locks the install out"
    )

    # ...so the next attempt is still the unauthenticated first-setup branch and
    # can finish the job, rather than 401ing forever.
    monkeypatch.setattr("app.auth.write_token_file", lambda _p, _t: "/tmp/admin.token")
    retry = asyncio.run(_handler(storage, conversations)(_request({"profile": "admin"})))
    assert retry.status_code == 200, retry.body
    assert storage.is_setup_complete() is True
