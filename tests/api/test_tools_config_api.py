"""PUT /api/tools/{id}/arguments and enum validation on .../variables.

Drives the route endpoints directly with a fake Request, backed by a real
``ToolRegistry`` on a temp SQLite DB. Pins:

- the ``…/arguments`` route is registered at all (regression for the dead
  ``cremind tools set-args`` route — the client always PUT this path, but the
  server never mounted it, so every call 404'd);
- argument values round-trip through ``ToolConfigManager`` (incl. non-string
  JSON), and bad keys / enum values / bodies are rejected;
- ``…/variables`` rejects a value outside a field's live ``dynamic_options``
  list (e.g. an invalid Claude Code model or permission mode) when the list
  resolves, unless ``allow_unknown`` is set, instead of silently persisting.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402 — registers tables on Base.metadata
from sqlalchemy import text  # noqa: E402

import app.api.tools as tools_api  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.tool_storage import ToolStorage  # noqa: E402
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult  # noqa: E402
from app.tools.builtin.tool import BuiltInToolGroup  # noqa: E402
from app.tools.config_manager import ToolConfigManager  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402


class _FakeLeaf(BuiltInTool):
    name = "noop"
    description = "fake leaf"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:  # pragma: no cover
        return BuiltInToolResult(structured_content={"ok": True})


_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "os": {"type": "string", "enum": ["linux", "windows"]},
        "timeout_s": {"type": "integer"},
    },
}


def _seed_profile(storage: ToolStorage, name: str) -> None:
    now = time.time() * 1000
    with storage._engine.begin() as conn:  # noqa: SLF001 — test seeding
        conn.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :name, :c, :u)"
            ),
            {"id": name, "name": name, "c": now, "u": now},
        )


def _make_registry(tmp_path: Path) -> ToolRegistry:
    provider = SqliteDatabaseProvider(str(tmp_path / "tools.db"))
    Base.metadata.create_all(bind=provider.sync_engine())
    storage = ToolStorage(provider)
    reg = ToolRegistry(storage, ToolConfigManager(storage))
    _seed_profile(storage, "admin")
    # A tool whose config_name maps to the real claude_code module, so the
    # handler resolves its real dynamic_options fields (CLAUDE_CODE_MODEL and
    # CLAUDE_CODE_PERMISSION_MODE) via the module's get_variable_options hook —
    # which the tests stub for hermeticity.
    cc = BuiltInToolGroup(
        config_name="claude_code", display_name="Claude Code",
        description="cc", functions=[_FakeLeaf()], llm=object(),
    )
    reg.register_builtin(cc, source="claude_code")
    # A tool carrying an arguments schema with an enum property.
    es = BuiltInToolGroup(
        config_name="exec_shell", display_name="Exec Shell",
        description="es", functions=[_FakeLeaf()], llm=object(),
        arguments_schema=_ARGS_SCHEMA,
    )
    reg.register_builtin(es, source="exec_shell")
    return reg


def _handler(state, path: str, method: str) -> Callable:
    for route in tools_api.get_tool_routes(state):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="admin", path_params=None, body=None, *, authed=True, query_params=None):
    async def _json():
        if body is _NO_BODY:
            raise ValueError("no body")
        return body if body is not None else {}
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authed, username=username),
        path_params=path_params or {},
        query_params=query_params or {},
        json=_json,
    )


_NO_BODY = object()


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.fixture(autouse=True)
def _silence_sse(monkeypatch):
    monkeypatch.setattr(tools_api, "publish_settings_state_changed", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _stub_variable_options(monkeypatch):
    """Keep set-var's dynamic-option check hermetic (no network / host creds / SDK).

    By default both the model and permission-mode option lists resolve to empty,
    which means "list unavailable" → any value is accepted. Tests that exercise
    the reject path re-stub ``get_variable_options`` with a non-empty list.
    """
    import app.tools.builtin.claude_code as cc_module

    async def _empty(*, variables, profile, refresh=False):
        return {
            "CLAUDE_CODE_MODEL": {"options": [], "error": None, "source": None},
            "CLAUDE_CODE_PERMISSION_MODE": {"options": [], "error": None, "source": None},
        }

    monkeypatch.setattr(cc_module, "get_variable_options", _empty)


def _stub_models(monkeypatch, ids):
    """Re-stub the claude_code model options with a non-empty list of ids."""
    import app.tools.builtin.claude_code as cc_module

    async def _fake(*, variables, profile, refresh=False):
        return {"CLAUDE_CODE_MODEL": {
            "options": [{"id": i, "label": i} for i in ids],
            "error": None, "source": "test",
        }}

    monkeypatch.setattr(cc_module, "get_variable_options", _fake)


def _stub_modes(monkeypatch, ids):
    """Re-stub the claude_code permission-mode options with a non-empty list."""
    import app.tools.builtin.claude_code as cc_module

    async def _fake(*, variables, profile, refresh=False):
        return {"CLAUDE_CODE_PERMISSION_MODE": {
            "options": [{"id": i, "label": i} for i in ids],
            "error": None, "source": "claude_agent_sdk",
        }}

    monkeypatch.setattr(cc_module, "get_variable_options", _fake)


# ── arguments route exists (bug-A regression) ───────────────────────────────

def test_arguments_route_registered(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    # Raises AssertionError if the route was never mounted.
    assert _handler(state, "/api/tools/{tool_id}/arguments", "PUT") is not None


# ── arguments happy path + round-trip ───────────────────────────────────────

def test_set_arguments_round_trips(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")

    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "exec_shell"},
        body={"arguments": {"os": "linux", "timeout_s": 120}},
    )))
    assert resp.status_code == 200
    saved = reg.config.get_arguments("exec_shell", "admin")
    assert saved == {"os": "linux", "timeout_s": 120}
    assert isinstance(saved["timeout_s"], int)  # non-string JSON round-trips


def test_set_arguments_rejects_unknown_key(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "exec_shell"},
        body={"arguments": {"bogus": 1}},
    )))
    assert resp.status_code == 400
    assert _body(resp)["key"] == "bogus"
    assert reg.config.get_arguments("exec_shell", "admin") == {}


def test_set_arguments_rejects_bad_enum(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "exec_shell"},
        body={"arguments": {"os": "solaris"}},
    )))
    assert resp.status_code == 400
    assert set(_body(resp)["allowed"]) == {"linux", "windows"}
    assert reg.config.get_arguments("exec_shell", "admin") == {}


def test_set_arguments_rejects_non_dict(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "exec_shell"},
        body={"arguments": ["not", "a", "dict"]},
    )))
    assert resp.status_code == 400


def test_set_arguments_unknown_tool_404(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "does_not_exist"},
        body={"arguments": {"os": "linux"}},
    )))
    assert resp.status_code == 404


def test_set_arguments_unauthenticated_401(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")
    resp = asyncio.run(handler(_req(
        authed=False, path_params={"tool_id": "exec_shell"},
        body={"arguments": {"os": "linux"}},
    )))
    assert resp.status_code == 401


def test_set_arguments_registry_none_503(tmp_path: Path) -> None:
    state = SimpleNamespace(registry=None)
    handler = _handler(state, "/api/tools/{tool_id}/arguments", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "exec_shell"},
        body={"arguments": {"os": "linux"}},
    )))
    assert resp.status_code == 503


# ── variables dynamic-option validation: permission mode ────────────────────

_MODE_IDS = ["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]


def test_set_variable_permission_mode_accepts_known(tmp_path: Path, monkeypatch) -> None:
    _stub_modes(monkeypatch, _MODE_IDS)
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_PERMISSION_MODE": "plan"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_PERMISSION_MODE"] == "plan"


def test_set_variable_permission_mode_rejects_unknown(tmp_path: Path, monkeypatch) -> None:
    _stub_modes(monkeypatch, _MODE_IDS)
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_PERMISSION_MODE": "bogus"}},
    )))
    assert resp.status_code == 400
    body = _body(resp)
    assert body["key"] == "CLAUDE_CODE_PERMISSION_MODE"
    assert "bypassPermissions" in body["allowed"]
    # Nothing persisted on rejection.
    assert reg.config.get_variables("claude_code", "admin") == {}


def test_set_variable_permission_mode_offline_accepts(tmp_path: Path) -> None:
    """When the SDK mode list can't be resolved (autouse stub → empty), the
    dynamic-option check is skipped and any value persists (graceful)."""
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_PERMISSION_MODE": "someFutureMode"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_PERMISSION_MODE"] == "someFutureMode"


def test_set_variable_permission_mode_force_accepts_unknown(tmp_path: Path, monkeypatch) -> None:
    _stub_modes(monkeypatch, _MODE_IDS)
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={
            "variables": {"CLAUDE_CODE_PERMISSION_MODE": "someFutureMode"},
            "allow_unknown": True,
        },
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_PERMISSION_MODE"] == "someFutureMode"


def test_set_variable_model_offline_accepts(tmp_path: Path) -> None:
    """When the live model list can't be resolved (autouse stub → empty), the
    dynamic-option check is skipped and any value persists (graceful)."""
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_MODEL": "claude-fable-5"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_MODEL"] == "claude-fable-5"


def test_set_variable_model_rejects_unknown(tmp_path: Path, monkeypatch) -> None:
    _stub_models(monkeypatch, ["claude-opus-4-8", "claude-sonnet-5", "opus"])
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_MODEL": "claude-3-opus-20240229"}},
    )))
    assert resp.status_code == 400
    body = _body(resp)
    assert body["key"] == "CLAUDE_CODE_MODEL"
    assert "claude-opus-4-8" in body["allowed"]
    # Nothing persisted on rejection.
    assert reg.config.get_variables("claude_code", "admin") == {}


def test_set_variable_model_accepts_known(tmp_path: Path, monkeypatch) -> None:
    _stub_models(monkeypatch, ["claude-opus-4-8", "claude-sonnet-5", "opus"])
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_MODEL": "claude-opus-4-8"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_MODEL"] == "claude-opus-4-8"


def test_set_variable_model_alias_accepted(tmp_path: Path, monkeypatch) -> None:
    _stub_models(monkeypatch, ["claude-opus-4-8", "opus"])
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_MODEL": "opus"}},
    )))
    assert resp.status_code == 200


def test_set_variable_model_allow_unknown_persists(tmp_path: Path, monkeypatch) -> None:
    _stub_models(monkeypatch, ["claude-opus-4-8"])
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={
            "variables": {"CLAUDE_CODE_MODEL": "my-custom-model"},
            "allow_unknown": True,
        },
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_MODEL"] == "my-custom-model"


# ── masked-secret round-trip guard ──────────────────────────────────────────
# A client (the Settings UI) reloads a secret as the mask "***" and re-submits
# the whole variables dict on save. Persisting "***" would clobber the real
# secret (and then get injected as a live credential). The handler must skip a
# secret whose incoming value is the mask, leaving the stored value intact.

def _vars_handler(tmp_path: Path):
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    return reg, _handler(state, "/api/tools/{tool_id}/variables", "PUT")


def test_secret_mask_does_not_clobber_stored_value(tmp_path: Path) -> None:
    reg, handler = _vars_handler(tmp_path)
    reg.config.set_variable(
        "claude_code", "admin", "CLAUDE_CODE_API_KEY", "sk-real-secret", is_secret=True,
    )
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_API_KEY": "***"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    # The real secret survives — the mask was skipped, not written.
    assert vars_["CLAUDE_CODE_API_KEY"] == "sk-real-secret"


def test_secret_real_value_persists(tmp_path: Path) -> None:
    reg, handler = _vars_handler(tmp_path)
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_API_KEY": "sk-brand-new"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_API_KEY"] == "sk-brand-new"


def test_secret_empty_string_clears(tmp_path: Path) -> None:
    reg, handler = _vars_handler(tmp_path)
    reg.config.set_variable(
        "claude_code", "admin", "CLAUDE_CODE_API_KEY", "sk-real-secret", is_secret=True,
    )
    # An explicit clear ("" — not the mask) must still write through.
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_API_KEY": ""}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_API_KEY"] == ""


def test_mask_skipped_while_other_vars_still_save(tmp_path: Path) -> None:
    reg, handler = _vars_handler(tmp_path)
    reg.config.set_variable(
        "claude_code", "admin", "CLAUDE_CODE_API_KEY", "sk-real-secret", is_secret=True,
    )
    # Mixed save (the UI's real behaviour): a changed non-secret rides along with
    # the untouched masked secret. The non-secret lands; the secret is preserved.
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {
            "CLAUDE_CODE_API_KEY": "***",
            "CLAUDE_CODE_MODEL": "claude-opus-4-8",
        }},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("claude_code", "admin", include_secrets=True)
    assert vars_["CLAUDE_CODE_API_KEY"] == "sk-real-secret"
    assert vars_["CLAUDE_CODE_MODEL"] == "claude-opus-4-8"


def test_secret_mask_guard_codex_parity(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    codex = BuiltInToolGroup(
        config_name="codex", display_name="Codex",
        description="cx", functions=[_FakeLeaf()], llm=object(),
    )
    reg.register_builtin(codex, source="codex")
    reg.config.set_variable(
        "codex", "admin", "CODEX_API_KEY", "sk-codex-real", is_secret=True,
    )
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "codex"},
        body={"variables": {"CODEX_API_KEY": "***"}},
    )))
    assert resp.status_code == 200
    vars_ = reg.config.get_variables("codex", "admin", include_secrets=True)
    assert vars_["CODEX_API_KEY"] == "sk-codex-real"


# ── variable options ────────────────────────────────────────────────────────

def test_variable_options_route_registered(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    assert _handler(state, "/api/tools/{tool_id}/variable-options", "GET") is not None


def test_variable_options_happy_path(tmp_path: Path, monkeypatch) -> None:
    import app.tools.builtin.claude_code as cc_module

    reg = _make_registry(tmp_path)
    # Seed a secret the hook should receive for credential resolution.
    reg.config.set_variable(
        "claude_code", "admin", "CLAUDE_CODE_API_KEY", "sk-test", is_secret=True,
    )
    seen: Dict[str, Any] = {}

    async def _fake(*, variables, profile, refresh=False):
        seen["variables"] = variables
        seen["profile"] = profile
        seen["refresh"] = refresh
        return {"CLAUDE_CODE_MODEL": {
            "options": [{"id": "claude-sonnet-4-5", "label": "Sonnet"}],
            "error": None,
            "source": "tool_variable_api_key",
        }}

    monkeypatch.setattr(cc_module, "get_variable_options", _fake)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variable-options", "GET")
    resp = asyncio.run(handler(_req(path_params={"tool_id": "claude_code"})))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["tool_id"] == "claude_code"
    opts = body["variables"]["CLAUDE_CODE_MODEL"]["options"]
    assert opts[0]["id"] == "claude-sonnet-4-5"
    # The hook received the stored secret + profile, and refresh defaulted False.
    assert seen["variables"].get("CLAUDE_CODE_API_KEY") == "sk-test"
    assert seen["profile"] == "admin"
    assert seen["refresh"] is False


def test_variable_options_refresh_param(tmp_path: Path, monkeypatch) -> None:
    import app.tools.builtin.claude_code as cc_module

    reg = _make_registry(tmp_path)
    seen: Dict[str, Any] = {}

    async def _fake(*, variables, profile, refresh=False):
        seen["refresh"] = refresh
        return {"CLAUDE_CODE_MODEL": {"options": [], "error": None, "source": None}}

    monkeypatch.setattr(cc_module, "get_variable_options", _fake)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variable-options", "GET")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"}, query_params={"refresh": "1"},
    )))
    assert resp.status_code == 200
    assert seen["refresh"] is True


def test_variable_options_tool_without_hook(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variable-options", "GET")
    # exec_shell exports no get_variable_options hook.
    resp = asyncio.run(handler(_req(path_params={"tool_id": "exec_shell"})))
    assert resp.status_code == 200
    assert _body(resp) == {"tool_id": "exec_shell", "variables": {}}


def test_variable_options_unknown_tool_404(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variable-options", "GET")
    resp = asyncio.run(handler(_req(path_params={"tool_id": "nope"})))
    assert resp.status_code == 404


def test_variable_options_unauthenticated_401(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variable-options", "GET")
    resp = asyncio.run(handler(_req(
        authed=False, path_params={"tool_id": "claude_code"},
    )))
    assert resp.status_code == 401


def test_variable_options_registry_none_503(tmp_path: Path) -> None:
    state = SimpleNamespace(registry=None)
    handler = _handler(state, "/api/tools/{tool_id}/variable-options", "GET")
    resp = asyncio.run(handler(_req(path_params={"tool_id": "claude_code"})))
    assert resp.status_code == 503
