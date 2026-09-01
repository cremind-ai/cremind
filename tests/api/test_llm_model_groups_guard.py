"""``PUT /api/llm/model-groups`` refuses to blank the main model.

``model_group.high`` is the single model everything resolves to — the
optional groups all fall back to it — so storing an empty one leaves the
profile unable to answer anything, and the channel paths that fail closed (the
group relevance judge) then go silent with nothing user-visible to explain it.

It is easy to send by accident: changing a section's Provider clears its
Model field, so Save right afterwards posts a blank. Same conventions as
``test_llm_codex_models_filtering`` — handlers resolved from the route table,
fake request objects, a dict-backed config storage.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable, Optional

from app.api import llm as llm_api

_ROUTE = "/api/llm/model-groups"


class _FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    def get(self, table: str, key: str, profile: str = "admin") -> Optional[str]:
        return self.rows.get((profile, key))

    def set(self, table: str, key: str, value: str, is_secret: bool = False,
            profile: str = "admin") -> None:
        self.rows[(profile, key)] = value

    def delete(self, table: str, key: str, profile: str = "admin") -> bool:
        return self.rows.pop((profile, key), None) is not None


def _request(body: Any, *, profile: str = "p1") -> object:
    async def _json() -> Any:
        return body

    return SimpleNamespace(
        headers={}, cookies={}, path_params={}, query_params={},
        user=SimpleNamespace(is_authenticated=True, username=profile),
        json=_json,
    )


def _handler(storage, monkeypatch) -> Callable:
    monkeypatch.setattr(llm_api, "require_auth_or_setup_mode", lambda req, cs: None)
    import app.events.settings_state_bus as bus
    monkeypatch.setattr(bus, "publish_settings_state_changed", lambda profile: None)
    state = SimpleNamespace(config_storage=storage)
    for route in llm_api.get_llm_routes(state):
        if route.path == _ROUTE and "PUT" in route.methods:
            return route.endpoint
    raise AssertionError(f"PUT {_ROUTE} not registered")


def _put(storage, monkeypatch, body: dict, *, profile: str = "p1"):
    return asyncio.run(_handler(storage, monkeypatch)(_request(body, profile=profile)))


def _body(resp) -> dict:
    return json.loads(resp.body)


def test_an_empty_main_model_is_rejected(monkeypatch) -> None:
    storage = _FakeConfigStorage()
    storage.set("llm_config", "model_group.high", "openai/gpt-5.4", profile="p1")

    resp = _put(storage, monkeypatch, {"model_groups": {"high": ""}})

    assert resp.status_code == 400
    assert "main model" in _body(resp)["error"].lower()
    # The working value is still there — a rejected save changes nothing.
    assert storage.get("llm_config", "model_group.high", profile="p1") == "openai/gpt-5.4"


def test_a_whitespace_main_model_is_rejected_too(monkeypatch) -> None:
    resp = _put(_FakeConfigStorage(), monkeypatch, {"model_groups": {"high": "   "}})
    assert resp.status_code == 400


def test_a_rejected_save_writes_nothing_at_all(monkeypatch) -> None:
    """Not a partial write: the other groups in the same payload must not land
    while the one that matters is refused."""
    storage = _FakeConfigStorage()

    _put(storage, monkeypatch, {
        "model_groups": {"high": "", "low": "openai/gpt-5.4-mini"},
        "default_provider": "openai",
        "vision_enabled": True,
    })

    assert storage.rows == {}


def test_clearing_an_optional_group_is_still_allowed(monkeypatch) -> None:
    """``low``/``plan``/``vision``/``audio`` fall back to the main model, so
    emptying one means "inherit" — a normal thing to save."""
    storage = _FakeConfigStorage()

    resp = _put(storage, monkeypatch, {
        "model_groups": {"high": "openai/gpt-5.4", "low": "", "plan": ""},
    })

    assert resp.status_code == 200
    assert storage.get("llm_config", "model_group.low", profile="p1") == ""


def test_a_payload_without_high_leaves_it_alone(monkeypatch) -> None:
    """An absent key means "don't touch", not "blank it" — partial saves from
    other callers must keep working."""
    storage = _FakeConfigStorage()
    storage.set("llm_config", "model_group.high", "openai/gpt-5.4", profile="p1")

    resp = _put(storage, monkeypatch, {"model_groups": {"vision": "openai/gpt-5.4"}})

    assert resp.status_code == 200
    assert storage.get("llm_config", "model_group.high", profile="p1") == "openai/gpt-5.4"


def test_a_valid_main_model_saves(monkeypatch) -> None:
    storage = _FakeConfigStorage()

    resp = _put(storage, monkeypatch, {
        "model_groups": {"high": "openai/gpt-5.4"},
        "reasoning_efforts": {"high": "medium"},
        "default_provider": "openai",
    })

    assert resp.status_code == 200
    assert storage.get("llm_config", "model_group.high", profile="p1") == "openai/gpt-5.4"
    assert storage.get("llm_config", "model_group.high.reasoning_effort", profile="p1") == "medium"


def test_the_guard_is_per_profile(monkeypatch) -> None:
    """Check #3: one profile's stored model must not satisfy another's save."""
    storage = _FakeConfigStorage()
    storage.set("llm_config", "model_group.high", "openai/gpt-5.4", profile="p1")

    resp = _put(storage, monkeypatch, {"model_groups": {"high": ""}}, profile="p2")

    assert resp.status_code == 400
    assert storage.get("llm_config", "model_group.high", profile="p2") is None
