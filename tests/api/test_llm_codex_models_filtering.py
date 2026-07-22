"""The provider-models endpoint and model_count filter by the active auth
method, and switching auth methods clears now-invalid model-group assignments
(app/api/llm.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable, Optional

from app.api import llm as llm_api

_CODEX_ONLY = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.3-codex-spark"}


class FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, bool]] = {}

    def get(self, table: str, key: str, profile: str = "admin") -> Optional[str]:
        row = self.rows.get((profile, key))
        return row[0] if row else None

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin") -> None:
        self.rows[(profile, key)] = (value, is_secret)

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        return self.rows.pop((profile, key), None) is not None

    def delete_by_prefix(self, table: str, prefix: str, profile: str = "admin") -> int:
        victims = [k for k in self.rows if k[0] == profile and k[1].startswith(prefix)]
        for k in victims:
            del self.rows[k]
        return len(victims)


def _make_request(*, body: Any = None, query: Optional[dict] = None,
                  name: Optional[str] = None, profile: str = "admin") -> object:
    async def _json() -> Any:
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        headers={}, cookies={},
        user=SimpleNamespace(is_authenticated=True, username=profile),
        path_params=({"name": name} if name is not None else {}),
        query_params=(query or {}),
        json=_json,
    )


def _handlers(storage, monkeypatch) -> dict[tuple[str, str], Callable]:
    monkeypatch.setattr(llm_api, "require_auth_or_setup_mode", lambda req, cs: None)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda profile: None)
    state = SimpleNamespace(config_storage=storage)
    return {(r.path, m): r.endpoint for r in llm_api.get_llm_routes(state) for m in r.methods}


def _body(resp) -> dict:
    return json.loads(resp.body)


def _model_ids(resp) -> set:
    return {m["id"] for m in _body(resp)["models"]}


def test_models_query_override_codex(monkeypatch):
    import asyncio
    h = _handlers(FakeConfigStorage(), monkeypatch)
    resp = asyncio.run(h[("/api/llm/providers/{name}/models", "GET")](
        _make_request(name="openai", query={"auth_method": "codex_oauth"})))
    ids = _model_ids(resp)
    assert _CODEX_ONLY.issubset(ids)
    assert "gpt-5.4" in ids  # shared model visible under both
    assert "o3" not in ids   # api_key-only hidden


def test_models_default_is_api_key(monkeypatch):
    import asyncio
    h = _handlers(FakeConfigStorage(), monkeypatch)
    resp = asyncio.run(h[("/api/llm/providers/{name}/models", "GET")](_make_request(name="openai")))
    ids = _model_ids(resp)
    assert _CODEX_ONLY.isdisjoint(ids)  # codex-only hidden by default
    assert "gpt-5.4" in ids and "o3" in ids


def test_models_stored_auth_method_honored(monkeypatch):
    import asyncio
    storage = FakeConfigStorage()
    storage.set("llm_config", "openai.auth_method", "codex_oauth")
    h = _handlers(storage, monkeypatch)
    resp = asyncio.run(h[("/api/llm/providers/{name}/models", "GET")](_make_request(name="openai")))
    assert _CODEX_ONLY.issubset(_model_ids(resp))


def test_model_count_reflects_active_method(monkeypatch):
    import asyncio
    storage = FakeConfigStorage()
    storage.set("llm_config", "openai.auth_method", "codex_oauth")
    h = _handlers(storage, monkeypatch)
    resp = asyncio.run(h[("/api/llm/providers", "GET")](_make_request()))
    openai = next(p for p in _body(resp)["providers"] if p["name"] == "openai")
    assert openai["model_count"] == 7  # 5 codex-only + 2 shared


def test_auth_switch_clears_stale_model_group(monkeypatch):
    import asyncio
    storage = FakeConfigStorage()
    storage.set("llm_config", "model_group.high", "openai/o3")        # api_key-only
    storage.set("llm_config", "model_group.low", "openai/gpt-5.4")    # shared → still valid
    h = _handlers(storage, monkeypatch)
    resp = asyncio.run(h[("/api/llm/providers/{name}", "PUT")](
        _make_request(name="openai", body={"auth_method": "codex_oauth"})))
    data = _body(resp)
    assert data["cleared_model_groups"] == ["high"]
    assert storage.get("llm_config", "model_group.high") is None
    assert storage.get("llm_config", "model_group.low") == "openai/gpt-5.4"
