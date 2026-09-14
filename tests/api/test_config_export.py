"""GET /api/config/export — who gets which file.

This is the one endpoint in the configuration-file story that is deliberately
open to every profile rather than admin-gated, so the interesting assertions are
all about scoping: the file is rendered for the *bearer token*, it carries that
profile's own channels, and a non-admin never sees the install-wide sections.

The renderer's own output shapes are covered in ``tests/config/test_config_export``;
what is under test here is the handler around it — the auth ladder, the format
and agent-URL query parameters, and the response headers a browser and the CLI
both depend on.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api import config as config_api
from app.config import runtime_env


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Describe a plain native install, whatever this machine really is.

    CI may itself run in a container, and the description is process-cached
    behind an lru_cache that feeds the agent's prompt — so both ends are
    cleared, exactly as the install-secrets tests do.
    """
    monkeypatch.setattr(runtime_env, "_CONTAINER_MARKER", tmp_path / "no-dockerenv")
    monkeypatch.setattr(runtime_env, "_SA_NAMESPACE_FILE", tmp_path / "no-namespace")
    monkeypatch.setattr(
        "app.config.tls_managed_env._CONTAINER_MARKER", tmp_path / "no-dockerenv")
    monkeypatch.setattr(
        "app.config.tls_managed_env._POD_MARKER", tmp_path / "no-serviceaccount")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(config_api.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(
        config_api.BaseConfig, "SQLITE_DB_PATH", str(tmp_path / "storage" / "cremind.db"),
        raising=False,
    )
    monkeypatch.setattr(config_api.BaseConfig, "APP_URL", "http://localhost:1515", raising=False)
    monkeypatch.setattr(
        "app.config.settings.get_user_working_directory", lambda: str(tmp_path / "work"),
    )
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.read_embedding_config",
        lambda _cs: {"enabled": True, "provider": "me5", "vectorstore": {"provider": "none"}},
    )
    monkeypatch.setattr("app.auth.verify_token", lambda token: {
        "profile": "li", "sub": "li", "exp": 1790000000, "iat": 1789000000,
    })
    runtime_env.describe_runtime_environment.cache_clear()
    yield
    runtime_env.describe_runtime_environment.cache_clear()


class _FakeConversationStorage:
    def __init__(self, channels=()) -> None:
        self._channels = list(channels)
        self.asked_for: list[str] = []

    async def list_channels(self, profile: str):
        self.asked_for.append(profile)
        return self._channels


def _handler(state) -> Callable:
    for route in config_api.get_config_routes(state):  # type: ignore[arg-type]
        if route.path == "/api/config/export":
            return route.endpoint
    raise AssertionError("/api/config/export route not registered")


def _state(channels=()) -> SimpleNamespace:
    return SimpleNamespace(
        storage_ready=True,
        config_storage=SimpleNamespace(is_setup_complete=lambda: True),
        conversation_storage=_FakeConversationStorage(channels),
    )


def _request(*, username="li", authenticated=True, token="eyJ-token", params=None, host="localhost:1515"):
    headers = {"host": host}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authenticated, username=username),
        headers=headers,
        query_params=params or {},
        cookies={},
    )


def _call(state, **kw):
    return asyncio.run(_handler(state)(_request(**kw)))


def _body(response) -> str:
    return response.body.decode("utf-8")


# ── the auth ladder ──────────────────────────────────────────────────────


def test_an_anonymous_caller_gets_401() -> None:
    response = _call(_state(), authenticated=False, username="", token=None)
    assert response.status_code == 401


def test_auth_is_checked_before_storage_readiness() -> None:
    """A 503 would confirm the server exists but has never been set up."""
    state = _state()
    state.storage_ready = False
    response = _call(state, authenticated=False, username="", token=None)
    assert response.status_code == 401


def test_a_missing_bearer_header_is_refused_even_when_the_user_looks_authenticated() -> None:
    """The token IS the file's payload — there is nothing to export without it."""
    response = _call(_state(), token=None)
    assert response.status_code == 401


def test_an_unknown_format_is_refused_by_name() -> None:
    response = _call(_state(), params={"format": "pdf"})
    assert response.status_code == 400
    assert "md, json, env" in json.loads(response.body)["error"]


# ── scope ────────────────────────────────────────────────────────────────


def test_a_non_admin_gets_a_reduced_file_and_says_so_in_a_header() -> None:
    response = _call(_state(), username="li", params={"format": "json"})
    assert response.status_code == 200
    assert response.headers["x-cremind-export-scope"] == "profile"
    snapshot = json.loads(_body(response))
    assert snapshot["scope"] == "profile"
    assert snapshot["profile"] == "li"
    assert snapshot["token"] == "eyJ-token", "the file is rendered for the bearer"
    assert snapshot["loginUrl"] == "http://localhost:1515/#/login/li"
    for install_wide in ("database", "vectorStore", "vnc", "kubernetes"):
        assert install_wide not in snapshot


def test_admin_gets_the_full_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.auth.verify_token", lambda token: {
        "profile": "admin", "sub": "admin", "exp": 1790000000,
    })
    monkeypatch.setattr(
        "app.config.config_export.gather_install_secrets",
        lambda: {"deployment": "native", "available": True, "db_provider": "sqlite"},
    )
    response = _call(_state(), username="admin", params={"format": "json"})
    assert response.status_code == 200
    assert response.headers["x-cremind-export-scope"] == "full"
    snapshot = json.loads(_body(response))
    assert "scope" not in snapshot, "the admin file is the one that has always existed"
    assert snapshot["database"]["provider"] == "sqlite"
    assert "vectorStore" in snapshot


def test_the_file_lists_the_callers_own_channels_and_hides_the_internal_one() -> None:
    state = _state(channels=[
        {"id": "ch_main", "channel_type": "main", "mode": "direct"},
        {"id": "ch_1", "channel_type": "telegram", "mode": "bot"},
    ])
    response = _call(state, params={"format": "json"})
    snapshot = json.loads(_body(response))
    assert snapshot["channels"] == [{"type": "telegram", "mode": "bot", "id": "ch_1"}]
    assert state.conversation_storage.asked_for == ["li"], "scoped to the bearer's profile"


def test_the_token_expiry_comes_from_the_tokens_own_claim() -> None:
    """UTC, to the second — the precision the ``exp`` claim actually has.
    Rendering it in the server's local zone would tell a user in another one
    that their token dies at the wrong hour."""
    import time

    expected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1790000000))
    response = _call(_state(), params={"format": "json"})
    assert json.loads(_body(response))["tokenExpiresAt"] == expected
    assert expected.endswith("Z")


# ── the address the file names ───────────────────────────────────────────


def test_the_agent_url_can_be_overridden_for_a_split_origin_install() -> None:
    """APP_URL is the internal bind on the documented development setup, so the
    login URL derived from it opens nothing. The caller that knows better says
    so."""
    response = _call(_state(), params={"format": "json", "agent_url": "http://localhost:8080"})
    snapshot = json.loads(_body(response))
    assert snapshot["agentUrl"] == "http://localhost:8080"
    assert snapshot["loginUrl"] == "http://localhost:8080/#/login/li"


def test_a_nonsense_agent_url_is_refused_rather_than_written_into_the_file() -> None:
    response = _call(_state(), params={"format": "json", "agent_url": "javascript:alert(1)"})
    assert response.status_code == 400
    assert "absolute http(s) URL" in json.loads(response.body)["error"]


def test_pending_https_is_carried_through_so_the_file_can_label_the_address() -> None:
    response = _call(_state(), params={
        "format": "md", "agent_url": "https://localhost:1515", "pending_https": "1",
    })
    assert "becomes active once setup finishes" in _body(response)


# ── what a browser and the CLI read off the response ─────────────────────


@pytest.mark.parametrize(
    ("fmt", "content_type"),
    [("md", "text/markdown"), ("json", "application/json"), ("env", "text/plain")],
)
def test_each_format_is_served_as_itself_and_named_as_a_download(fmt, content_type) -> None:
    response = _call(_state(), params={"format": fmt})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["content-disposition"] == (
        f'attachment; filename="cremind-li-config.{fmt}"'
    )


def test_markdown_is_the_default_format() -> None:
    response = _call(_state())
    assert response.headers["content-type"].startswith("text/markdown")
    assert _body(response).startswith("# Cremind Configuration — li")


def test_the_response_is_never_cached() -> None:
    """It carries a live JWT; a proxy holding a copy is a credential leak."""
    response = _call(_state())
    assert response.headers["cache-control"] == "no-store"
