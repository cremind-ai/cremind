"""Unit tests for the ``codex`` account model listing + credential resolution.

Covers ``codex_runner.resolve_auth`` / ``credential_source`` (the credential
tiers), and ``list_models`` success / failure / caching with the Codex SDK's
``models()`` faked (no real SDK / binary / network), plus the
``get_variable_options`` model-field shape.

Tests drive coroutines with ``asyncio.run`` (matching the repo's other tool
tests — no pytest-asyncio needed).
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

import app.tools.builtin.codex as codex
import app.tools.builtin.codex_runner as runner
from app.tools.builtin.codex_runner import Var


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Clear the module cache and neutralise ambient credentials so each test
    controls the credential chain explicitly."""
    runner._models_cache.clear()
    monkeypatch.setattr(runner.BaseConfig, "get_provider_api_key", lambda *a, **k: None)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(runner, "_CODEX_AUTH_PATH", tmp_path / "nope.json")
    # Keep the managed CODEX_HOME out of the real system dir (per-credential now).
    monkeypatch.setattr(
        runner, "_managed_codex_home",
        lambda auth: tmp_path / "codex-home" / runner._cache_key(auth)[:16],
    )
    yield
    runner._models_cache.clear()


def _install_models_sdk(monkeypatch, *, rows=None, raise_exc=None, counter=None):
    """Install a fake ``openai_codex`` whose ``AsyncCodex.models()`` returns
    ``rows`` (a ModelListResponse-like object) or raises ``raise_exc``."""
    from dataclasses import dataclass

    mod = types.ModuleType("openai_codex")

    @dataclass
    class CodexConfig:
        codex_bin: object = None
        config_overrides: tuple = ()
        cwd: object = None
        env: object = None

    mod.CodexConfig = CodexConfig

    class AsyncCodex:
        def __init__(self, config=None):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def login_api_key(self, key):
            pass

        async def models(self):
            if counter is not None:
                counter["n"] += 1
            if raise_exc is not None:
                raise raise_exc
            return SimpleNamespace(data=rows or [])

    mod.AsyncCodex = AsyncCodex
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    return mod


# ── resolve_auth / credential_source: credential tiers ──────────────────────

def test_auth_tool_variable_api_key():
    auth = runner.resolve_auth({Var.API_KEY: "sk-explicit"}, "admin")
    assert auth.api_key == "sk-explicit"
    assert auth.source == "tool_variable_api_key"
    assert "CODEX_HOME" in auth.env_overrides


def test_auth_profile_openai_key(monkeypatch):
    monkeypatch.setattr(runner.BaseConfig, "get_provider_api_key", lambda *a, **k: "sk-profile")
    auth = runner.resolve_auth({}, "admin")
    assert auth.api_key == "sk-profile"
    assert auth.source == "profile_openai_api_key"


def test_auth_env_codex_key(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "sk-env-codex")
    auth = runner.resolve_auth({}, "admin")
    assert auth.api_key == "sk-env-codex"
    assert auth.source == "env_codex_api_key"


def test_auth_env_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-openai")
    auth = runner.resolve_auth({}, "admin")
    assert auth.api_key == "sk-env-openai"
    assert auth.source == "env_openai_api_key"


def test_auth_host_codex_login(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"tokens": {"access_token": "x"}}')
    monkeypatch.setattr(runner, "_CODEX_AUTH_PATH", auth_file)
    auth = runner.resolve_auth({}, "admin")
    assert auth.api_key is None
    assert auth.source == "host_codex_login"
    assert auth.env_overrides == {}  # host login uses the ambient CODEX_HOME


def test_credential_source_none_when_absent():
    # The _isolate fixture points the auth path at a nonexistent file.
    assert runner.credential_source({}, "admin") is None


def test_credential_source_host_file(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"OPENAI_API_KEY": "host-key"}')
    monkeypatch.setattr(runner, "_CODEX_AUTH_PATH", auth_file)
    assert runner.credential_source({}, "admin") == "host_codex_login"


# ── list_models: success / failure / caching ────────────────────────────────

def test_list_models_success(monkeypatch):
    rows = [
        SimpleNamespace(id="gpt-5.1-codex", display_name="GPT-5.1 Codex", hidden=False),
        SimpleNamespace(id="gpt-5.1-codex-mini", display_name=None, hidden=False),
        SimpleNamespace(id="hidden-model", display_name="Hidden", hidden=True),
    ]
    _install_models_sdk(monkeypatch, rows=rows)
    out = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert out["source"] == "tool_variable_api_key"
    assert out["cached"] is False
    ids = [m["id"] for m in out["models"]]
    assert ids == ["gpt-5.1-codex", "gpt-5.1-codex-mini"]  # hidden filtered out
    assert out["models"][1]["display_name"] == "gpt-5.1-codex-mini"  # id fallback


def test_list_models_sdk_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    out = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert out["models"] == []
    assert "cremind features install codex" in out["error"]


def test_list_models_no_credential_returns_error(monkeypatch):
    _install_models_sdk(monkeypatch, raise_exc=RuntimeError("not logged in"))
    out = asyncio.run(runner.list_models({}, "admin"))
    assert out["models"] == []
    assert "credential" in out["error"].lower()
    assert out["source"] is None


def test_list_models_fetch_failure_returns_error(monkeypatch):
    _install_models_sdk(monkeypatch, raise_exc=RuntimeError("app-server crashed"))
    out = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert out["models"] == []
    assert "app-server crashed" in out["error"]
    # A failure is never cached.
    assert not runner._models_cache


def test_list_models_uses_cache(monkeypatch):
    counter = {"n": 0}
    _install_models_sdk(
        monkeypatch, rows=[SimpleNamespace(id="m1", display_name="M1", hidden=False)],
        counter=counter,
    )
    first = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    second = asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    assert counter["n"] == 1
    assert first["cached"] is False
    assert second["cached"] is True


def test_list_models_force_refresh_bypasses_cache(monkeypatch):
    counter = {"n": 0}
    _install_models_sdk(
        monkeypatch, rows=[SimpleNamespace(id="m1", display_name="M1", hidden=False)],
        counter=counter,
    )
    asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin"))
    asyncio.run(runner.list_models({Var.API_KEY: "sk"}, "admin", force_refresh=True))
    assert counter["n"] == 2


def test_list_models_cache_isolated_per_credential(monkeypatch):
    counter = {"n": 0}
    _install_models_sdk(
        monkeypatch, rows=[SimpleNamespace(id="m1", display_name="M1", hidden=False)],
        counter=counter,
    )
    asyncio.run(runner.list_models({Var.API_KEY: "sk-a"}, "admin"))
    asyncio.run(runner.list_models({Var.API_KEY: "sk-b"}, "admin"))
    # Different credentials -> separate cache entries -> two fetches.
    assert counter["n"] == 2


# ── get_variable_options hook shape ─────────────────────────────────────────

def test_get_variable_options_model_field(monkeypatch):
    async def _fake_list(variables, profile, *, force_refresh=False):
        return {
            "models": [{"id": "gpt-5.1-codex", "display_name": "Codex"}],
            "source": "tool_variable_api_key",
            "cached": False,
        }

    monkeypatch.setattr(runner, "list_models", _fake_list)
    # Sandbox introspection needs a Sandbox enum present.
    import enum
    mod = types.ModuleType("openai_codex")
    mod.Sandbox = enum.Enum("Sandbox", {"full_access": "full-access"}, type=str)
    monkeypatch.setitem(sys.modules, "openai_codex", mod)

    out = asyncio.run(codex.get_variable_options(variables={}, profile="admin"))
    field = out[Var.MODEL]
    ids = [o["id"] for o in field["options"]]
    assert ids == ["gpt-5.1-codex"]
    assert field["error"] is None
    assert field["source"] == "tool_variable_api_key"


def test_get_variable_options_empty_model_list(monkeypatch):
    async def _empty(variables, profile, *, force_refresh=False):
        return {"models": [], "error": "no creds", "source": None}

    monkeypatch.setattr(runner, "list_models", _empty)
    import enum
    mod = types.ModuleType("openai_codex")
    mod.Sandbox = enum.Enum("Sandbox", {"full_access": "full-access"}, type=str)
    monkeypatch.setitem(sys.modules, "openai_codex", mod)

    out = asyncio.run(codex.get_variable_options(variables={}, profile="admin"))
    field = out[Var.MODEL]
    assert field["options"] == []
    assert field["error"] == "no creds"
