"""PUT /api/tools/{id}/arguments and enum validation on .../variables.

Drives the route endpoints directly with a fake Request, backed by a real
``ToolRegistry`` on a temp SQLite DB. Pins:

- the ``…/arguments`` route is registered at all (regression for the dead
  ``cremind tools set-args`` route — the client always PUT this path, but the
  server never mounted it, so every call 404'd);
- argument values round-trip through ``ToolConfigManager`` (incl. non-string
  JSON), and bad keys / enum values / bodies are rejected;
- ``…/variables`` rejects a value outside a field's declared ``enum``
  (e.g. an invalid Claude Code permission mode) instead of silently persisting.
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
    # handler resolves its real CLAUDE_CODE_PERMISSION_MODE enum.
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


def _req(username="admin", path_params=None, body=None, *, authed=True):
    async def _json():
        if body is _NO_BODY:
            raise ValueError("no body")
        return body if body is not None else {}
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authed, username=username),
        path_params=path_params or {},
        json=_json,
    )


_NO_BODY = object()


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.fixture(autouse=True)
def _silence_sse(monkeypatch):
    monkeypatch.setattr(tools_api, "publish_settings_state_changed", lambda *a, **k: None)


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


# ── variables enum validation ───────────────────────────────────────────────

def test_set_variable_valid_enum_persists(tmp_path: Path) -> None:
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


def test_set_variable_bad_enum_rejected(tmp_path: Path) -> None:
    reg = _make_registry(tmp_path)
    state = SimpleNamespace(registry=reg)
    handler = _handler(state, "/api/tools/{tool_id}/variables", "PUT")
    resp = asyncio.run(handler(_req(
        path_params={"tool_id": "claude_code"},
        body={"variables": {"CLAUDE_CODE_PERMISSION_MODE": "bogus"}},
    )))
    assert resp.status_code == 400
    assert "bypassPermissions" in _body(resp)["allowed"]
    # Nothing persisted on rejection.
    assert reg.config.get_variables("claude_code", "admin") == {}
