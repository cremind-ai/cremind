"""Tests for the custom-provider CRUD on ``/api/llm/*``.

A "custom provider" is a user-defined OpenAI-API-compatible endpoint added
from the LLM Providers page. It is stored per-profile in the ``llm_config``
table (no new table/migration): the definition in a single non-secret JSON row
``custom_providers`` and the API key in a secret row ``custom:<slug>.api_key``.

These tests pin the handler contract end to end against an in-memory config
store: create → list → models → update → delete, plus the edge cases that make
the design safe — slug de-dup, the ``"***"`` redaction sentinel not clobbering a
stored key, delete stripping the registry entry + dangling model-group refs, and
``delete_by_prefix`` not over-matching a sibling slug.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

from app.api import llm as llm_api


class FakeConfigStorage:
    """Minimal in-memory stand-in for ``DynamicConfigStorage`` (llm_config)."""

    def __init__(self) -> None:
        # (profile, key) -> (value, is_secret)
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


def _make_request(*, body: Any = None, name: Optional[str] = None,
                  authenticated: bool = True, profile: str = "admin") -> object:
    async def _json() -> Any:
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        headers={},
        cookies={},
        user=SimpleNamespace(is_authenticated=authenticated, username=profile),
        path_params=({"name": name} if name is not None else {}),
        json=_json,
    )


def _handlers(storage: FakeConfigStorage, monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], Callable]:
    """Register the LLM routes with a stub state and index by (path, method)."""
    monkeypatch.setattr(llm_api, "require_auth_or_setup_mode", lambda req, cs: None)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda profile: None)
    state = SimpleNamespace(config_storage=storage)
    out: dict[tuple[str, str], Callable] = {}
    for r in llm_api.get_llm_routes(state):  # type: ignore[arg-type]
        for m in r.methods:
            out[(r.path, m)] = r.endpoint
    return out


def _body(resp) -> dict:
    return json.loads(resp.body)


# ── create ────────────────────────────────────────────────────────────────

def test_create_custom_provider_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)

    create = h[("/api/llm/providers/custom", "POST")]
    resp = asyncio.run(create(_make_request(body={
        "display_name": "My LLM",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-secret",
        "models": [
            {"id": "big", "display_name": "Big", "vision": True,
             "input_price_per_1m": 2.5, "output_price_per_1m": 10,
             "cache_read_price_per_1m": 0.25, "cache_write_price_per_1m": 3.0},
            {"id": "small", "supports_reasoning": True},
        ],
    })))
    assert resp.status_code == 200, resp.body
    assert _body(resp)["name"] == "custom:my-llm"

    # Registry row is non-secret; key row is secret.
    assert storage.rows[("admin", "custom_providers")][1] is False
    assert storage.rows[("admin", "custom:my-llm.api_key")] == ("sk-secret", True)
    registry = json.loads(storage.rows[("admin", "custom_providers")][0])
    assert registry["my-llm"]["base_url"] == "https://api.example.com/v1"
    assert len(registry["my-llm"]["models"]) == 2
    # Reasoning is a yes/no flag; when on it exposes the standard effort levels.
    # A blank price is omitted while entered prices (incl. cache) are kept.
    small_stored = next(m for m in registry["my-llm"]["models"] if m["id"] == "small")
    assert small_stored["supports_reasoning"] is True
    assert small_stored["reasoning_effort"] == ["low", "medium", "high"]
    assert "input_price_per_1m" not in small_stored
    big_stored = next(m for m in registry["my-llm"]["models"] if m["id"] == "big")
    assert big_stored["input_price_per_1m"] == 2.5
    assert big_stored["cache_read_price_per_1m"] == 0.25
    assert big_stored["cache_write_price_per_1m"] == 3.0
    assert big_stored["supports_reasoning"] is False

    # List shows it as a configured custom provider.
    list_providers = h[("/api/llm/providers", "GET")]
    providers = _body(asyncio.run(list_providers(_make_request())))["providers"]
    entry = next(p for p in providers if p["name"] == "custom:my-llm")
    assert entry["is_custom"] is True
    assert entry["configured"] is True
    assert entry["base_url"] == "https://api.example.com/v1"
    assert entry["model_count"] == 2

    # Models endpoint surfaces the standard effort list, vision, and all prices.
    models_h = h[("/api/llm/providers/{name}/models", "GET")]
    models = _body(asyncio.run(models_h(_make_request(name="custom:my-llm"))))["models"]
    big = next(m for m in models if m["id"] == "big")
    small = next(m for m in models if m["id"] == "small")
    assert big["vision"] is True
    assert "reasoning_effort" not in big
    assert big["input_price_per_1m"] == 2.5
    assert big["cache_read_price_per_1m"] == 0.25
    assert big["cache_write_price_per_1m"] == 3.0
    assert small["reasoning_effort"] == ["low", "medium", "high"]
    assert small["input_price_per_1m"] is None  # blank price → unknown cost


@pytest.mark.parametrize("body,needle", [
    ({"base_url": "u", "models": [{"id": "m"}]}, "name is required"),
    ({"display_name": "X", "models": [{"id": "m"}]}, "Base URL is required"),
    ({"display_name": "X", "base_url": "u", "models": []}, "at least one model"),
    ({"display_name": "X", "base_url": "u", "models": [{"id": ""}]}, "at least one model"),
])
def test_create_validation(monkeypatch: pytest.MonkeyPatch, body: dict, needle: str) -> None:
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    create = h[("/api/llm/providers/custom", "POST")]
    resp = asyncio.run(create(_make_request(body=body)))
    assert resp.status_code == 400, resp.body
    assert needle in _body(resp)["error"]


def test_slug_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    create = h[("/api/llm/providers/custom", "POST")]
    base = {"base_url": "u", "models": [{"id": "m"}]}
    r1 = asyncio.run(create(_make_request(body={**base, "display_name": "Acme"})))
    r2 = asyncio.run(create(_make_request(body={**base, "display_name": "Acme"})))
    assert _body(r1)["name"] == "custom:acme"
    assert _body(r2)["name"] == "custom:acme-2"


# ── update ──────────────────────────────────────────────────────────────

def _seed_provider(storage: FakeConfigStorage, slug: str = "my-llm", api_key: str = "sk-orig") -> None:
    registry = {slug: {
        "display_name": "Orig",
        "base_url": "https://orig/v1",
        "models": [{"id": "m1", "display_name": "M1", "group_hint": "high",
                    "vision": False, "supports_reasoning": False, "reasoning_effort": []}],
    }}
    storage.set("llm_config", "custom_providers", json.dumps(registry), profile="admin")
    storage.set("llm_config", f"custom:{slug}.api_key", api_key, is_secret=True, profile="admin")


def test_update_merges_definition_and_preserves_key(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    _seed_provider(storage)
    h = _handlers(storage, monkeypatch)
    update = h[("/api/llm/providers/{name}", "PUT")]

    # No api_key in body → stored key preserved; definition fields merged.
    resp = asyncio.run(update(_make_request(name="custom:my-llm", body={
        "display_name": "Renamed",
        "base_url": "https://new/v2",
        "models": [{"id": "m2", "group_hint": "low"}],
    })))
    assert resp.status_code == 200, resp.body
    assert storage.rows[("admin", "custom:my-llm.api_key")][0] == "sk-orig"
    registry = json.loads(storage.rows[("admin", "custom_providers")][0])
    assert registry["my-llm"]["display_name"] == "Renamed"
    assert registry["my-llm"]["base_url"] == "https://new/v2"
    assert registry["my-llm"]["models"][0]["id"] == "m2"


def test_update_redaction_sentinel_does_not_clobber_key(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    _seed_provider(storage)
    h = _handlers(storage, monkeypatch)
    update = h[("/api/llm/providers/{name}", "PUT")]
    asyncio.run(update(_make_request(name="custom:my-llm", body={"api_key": "***"})))
    assert storage.rows[("admin", "custom:my-llm.api_key")][0] == "sk-orig"

    # A real new key does replace it.
    asyncio.run(update(_make_request(name="custom:my-llm", body={"api_key": "sk-new"})))
    assert storage.rows[("admin", "custom:my-llm.api_key")][0] == "sk-new"


def test_update_unknown_custom_slug_404(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    h = _handlers(storage, monkeypatch)
    update = h[("/api/llm/providers/{name}", "PUT")]
    resp = asyncio.run(update(_make_request(name="custom:nope", body={"base_url": "u"})))
    assert resp.status_code == 404, resp.body


def test_update_rejects_empty_model_list(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    _seed_provider(storage)
    h = _handlers(storage, monkeypatch)
    update = h[("/api/llm/providers/{name}", "PUT")]
    resp = asyncio.run(update(_make_request(name="custom:my-llm", body={"models": [{"id": ""}]})))
    assert resp.status_code == 400, resp.body


# ── delete ──────────────────────────────────────────────────────────────

def test_delete_removes_registry_key_and_model_group(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    _seed_provider(storage)
    storage.set("llm_config", "model_group.high", "custom:my-llm/m1", profile="admin")
    storage.set("llm_config", "model_group.low", "groq/other", profile="admin")
    h = _handlers(storage, monkeypatch)
    delete = h[("/api/llm/providers/{name}/config", "DELETE")]

    resp = asyncio.run(delete(_make_request(name="custom:my-llm")))
    assert resp.status_code == 200, resp.body
    # api_key row gone, registry entry gone.
    assert ("admin", "custom:my-llm.api_key") not in storage.rows
    assert json.loads(storage.rows[("admin", "custom_providers")][0]) == {}
    # The dangling model-group ref is cleared; the unrelated one is untouched.
    assert ("admin", "model_group.high") not in storage.rows
    assert storage.rows[("admin", "model_group.low")][0] == "groq/other"


def test_delete_by_prefix_does_not_overmatch_sibling(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeConfigStorage()
    registry = {
        "acme": {"display_name": "Acme", "base_url": "u", "models": [{"id": "m"}]},
        "acme-2": {"display_name": "Acme", "base_url": "u", "models": [{"id": "m"}]},
    }
    storage.set("llm_config", "custom_providers", json.dumps(registry), profile="admin")
    storage.set("llm_config", "custom:acme.api_key", "k1", is_secret=True, profile="admin")
    storage.set("llm_config", "custom:acme-2.api_key", "k2", is_secret=True, profile="admin")
    h = _handlers(storage, monkeypatch)
    delete = h[("/api/llm/providers/{name}/config", "DELETE")]

    asyncio.run(delete(_make_request(name="custom:acme")))
    # Sibling's key + registry entry survive.
    assert storage.rows[("admin", "custom:acme-2.api_key")][0] == "k2"
    assert set(json.loads(storage.rows[("admin", "custom_providers")][0])) == {"acme-2"}
