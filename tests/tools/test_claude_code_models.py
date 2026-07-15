"""Unit tests for the ``claude_code`` account model listing.

Covers ``claude_code_runner`` credential-header resolution, the ``list_models``
success / failure / caching behaviour (with the HTTP layer stubbed), the
``_fetch_models`` URL + headers via ``httpx.MockTransport``, and the
``get_variable_options`` hook shape. No real network / SDK.

Tests drive coroutines with ``asyncio.run`` (matching the repo's other tool
tests — no pytest-asyncio needed).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.tools.builtin.claude_code as claude_code
import app.tools.builtin.claude_code_runner as runner
from app.tools.builtin.claude_code_runner import Var


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Clear the module cache and neutralise ambient credentials so each test
    controls the credential chain explicitly."""
    runner._models_cache.clear()
    monkeypatch.setattr(runner, "get_dynamic", lambda *a, **k: None)
    monkeypatch.setattr(
        runner.BaseConfig, "get_provider_api_key", lambda *a, **k: None,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(runner, "_CLAUDE_CREDENTIALS_PATH", tmp_path / "nope.json")
    yield
    runner._models_cache.clear()


# ── _build_models_headers: credential tiers ─────────────────────────────────

def test_headers_tool_variable_api_key():
    headers, source = runner._build_models_headers(
        {Var.API_KEY: "sk-explicit"}, "admin",
    )
    assert headers["x-api-key"] == "sk-explicit"
    assert headers["anthropic-version"] == runner._ANTHROPIC_VERSION
    assert "Authorization" not in headers
    assert source == "tool_variable_api_key"


def test_headers_profile_setup_token(monkeypatch):
    def _dyn(table, key, *a, **k):
        if key == "anthropic.auth_method":
            return "setup_token"
        if key == "anthropic.setup_token":
            return "oauth-tok"
        return None

    monkeypatch.setattr(runner, "get_dynamic", _dyn)
    headers, source = runner._build_models_headers({}, "admin")
    assert headers["Authorization"] == "Bearer oauth-tok"
    assert headers["anthropic-beta"] == runner._OAUTH_BETA
    assert "x-api-key" not in headers
    assert source == "profile_setup_token"


def test_headers_profile_api_key(monkeypatch):
    monkeypatch.setattr(
        runner.BaseConfig, "get_provider_api_key", lambda *a, **k: "sk-profile",
    )
    headers, source = runner._build_models_headers({}, "admin")
    assert headers["x-api-key"] == "sk-profile"
    assert source == "profile_api_key"


def test_headers_env_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    headers, source = runner._build_models_headers({}, "admin")
    assert headers["x-api-key"] == "sk-env"
    assert source == "env_anthropic_api_key"


def test_headers_env_oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-env")
    headers, source = runner._build_models_headers({}, "admin")
    assert headers["Authorization"] == "Bearer tok-env"
    assert source == "env_oauth_token"


def test_headers_host_credentials_file(monkeypatch, tmp_path):
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "host-tok"}}))
    monkeypatch.setattr(runner, "_CLAUDE_CREDENTIALS_PATH", cred)
    headers, source = runner._build_models_headers({}, "admin")
    assert headers["Authorization"] == "Bearer host-tok"
    assert source == "host_claude_login"


def test_headers_none_when_no_credential():
    headers, source = runner._build_models_headers({}, "admin")
    assert headers == {}
    assert source is None


def test_credential_source_host_file(monkeypatch, tmp_path):
    """credential_source() must recognise the host claude-login store too, so the
    status leaf and the model listing agree on what's visible."""
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "host-tok"}}))
    monkeypatch.setattr(runner, "_CLAUDE_CREDENTIALS_PATH", cred)
    assert runner.credential_source({}, "admin") == "host_claude_login"


def test_credential_source_none_when_absent():
    # The _isolate fixture points the credentials path at a nonexistent file.
    assert runner.credential_source({}, "admin") is None


# ── list_models: success / failure / caching ────────────────────────────────

def test_list_models_success(monkeypatch):
    async def _fake_fetch(headers):
        return [
            {"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5"},
            {"id": "claude-opus-4-5"},  # no display_name -> falls back to id
        ]

    monkeypatch.setattr(runner, "_fetch_models", _fake_fetch)
    out = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert out["source"] == "tool_variable_api_key"
    assert out["cached"] is False
    ids = [m["id"] for m in out["models"]]
    assert ids == ["claude-sonnet-4-5", "claude-opus-4-5"]
    assert out["models"][1]["display_name"] == "claude-opus-4-5"


def test_list_models_no_credential_returns_error():
    out = asyncio.run(runner.list_models({}, "admin"))
    assert out["models"] == []
    assert "credential" in out["error"].lower()
    assert out["source"] is None


def test_list_models_fetch_failure_returns_error(monkeypatch):
    async def _boom(headers):
        raise RuntimeError("network down")

    monkeypatch.setattr(runner, "_fetch_models", _boom)
    out = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert out["models"] == []
    assert "network down" in out["error"]
    # A failure is never cached.
    assert not runner._models_cache


def test_list_models_http_401_labelled(monkeypatch):
    async def _rejected(headers):
        request = httpx.Request("GET", runner._MODELS_URL)
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(runner, "_fetch_models", _rejected)
    out = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert out["models"] == []
    assert "401" in out["error"]
    assert "rejected" in out["error"].lower()


def test_list_models_uses_cache(monkeypatch):
    calls = {"n": 0}

    async def _counting(headers):
        calls["n"] += 1
        return [{"id": "m1"}]

    monkeypatch.setattr(runner, "_fetch_models", _counting)
    first = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    second = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert calls["n"] == 1
    assert first["cached"] is False
    assert second["cached"] is True


def test_list_models_force_refresh_bypasses_cache(monkeypatch):
    calls = {"n": 0}

    async def _counting(headers):
        calls["n"] += 1
        return [{"id": "m1"}]

    monkeypatch.setattr(runner, "_fetch_models", _counting)
    asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin", force_refresh=True))
    assert calls["n"] == 2


def test_list_models_cache_isolated_per_credential(monkeypatch):
    calls = {"n": 0}

    async def _counting(headers):
        calls["n"] += 1
        return [{"id": f"m{calls['n']}"}]

    monkeypatch.setattr(runner, "_fetch_models", _counting)
    asyncio.run(runner.list_models({Var.API_KEY: "sk-a"}, "admin"))
    asyncio.run(runner.list_models({Var.API_KEY: "sk-b"}, "admin"))
    # Different credentials -> separate cache entries -> two fetches.
    assert calls["n"] == 2


# ── _fetch_models: URL + headers over a mock transport ──────────────────────

def test_fetch_models_url_and_headers(monkeypatch):
    captured = {}

    def _handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": [{"id": "m1", "display_name": "M1"}]})

    transport = httpx.MockTransport(_handle)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(runner.httpx, "AsyncClient", _factory)
    rows = asyncio.run(runner._fetch_models(
        {"x-api-key": "sk", "anthropic-version": runner._ANTHROPIC_VERSION}
    ))
    assert rows == [{"id": "m1", "display_name": "M1"}]
    assert captured["url"].startswith(runner._MODELS_URL)
    assert "limit=1000" in captured["url"]
    assert captured["headers"]["x-api-key"] == "sk"


# ── get_variable_options hook shape ─────────────────────────────────────────

def test_get_variable_options_appends_aliases(monkeypatch):
    async def _fake_list(variables, profile, *, force_refresh=False):
        return {
            "models": [{"id": "claude-sonnet-4-5", "display_name": "Sonnet"}],
            "source": "tool_variable_api_key",
            "cached": False,
        }

    monkeypatch.setattr(runner, "list_models", _fake_list)
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    field = out[Var.MODEL]
    ids = [o["id"] for o in field["options"]]
    assert "claude-sonnet-4-5" in ids
    # Aliases are appended once the account list resolved.
    assert set(runner._MODEL_ALIASES).issubset(set(ids))
    assert field["error"] is None
    assert field["source"] == "tool_variable_api_key"


def test_get_variable_options_no_aliases_on_empty(monkeypatch):
    async def _empty(variables, profile, *, force_refresh=False):
        return {"models": [], "error": "no creds", "source": None}

    monkeypatch.setattr(runner, "list_models", _empty)
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    field = out[Var.MODEL]
    assert field["options"] == []  # no aliases when the account list is empty
    assert field["error"] == "no creds"
