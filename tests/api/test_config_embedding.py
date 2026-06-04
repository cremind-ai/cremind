"""Tests for the embedding preflight on PUT /api/config/embedding.

The Setup Wizard installs the optional pip extras for whichever
embedding/vectorstore providers the user picked. A user who disabled
Vector Embedding at setup later has none of those extras on disk; when
they re-enable from Settings → Vector Embedding the apply step would
crash with ``ModuleNotFoundError: sentence_transformers``. The handler
guards against that by preflight-checking the required features and
returning ``409 FeatureNotInstalled`` so the frontend can drive the
install via SSE and retry.

These tests pin the preflight contract:

- enabled=True with missing extras → 409 with a ``missing`` array
  enumerating every required feature (embedding provider + vector store).
- enabled=False → no preflight (the disabled path is harmless and we
  don't want to force an install just to flip a flag off).
- enabled=True with everything installed → preflight passes through to
  the existing persist + apply path.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.api import config as config_api


def _get_handler() -> Callable:
    """Pull ``handle_put_embedding_config`` from the route closure.

    Same approach as ``tests/api/test_install_secrets.py`` — the handler
    captures ``state`` via closure, so we register the routes once with
    a stub state and look up the PUT endpoint by path + method.
    """
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=SimpleNamespace(),
        conversation_storage=SimpleNamespace(),
    )
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    for route in routes:
        if route.path == "/api/config/embedding" and "PUT" in route.methods:
            return route.endpoint
    raise AssertionError("PUT /api/config/embedding route not registered")


def _stub_admin_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)


def _stub_embedding_state_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the embedding lifecycle is idle — not busy, not in any
    transient state. Otherwise the handler short-circuits with 409
    "Embedding subsystem is currently …; please wait." before reaching
    the preflight we're trying to test.
    """
    from app.config import embedding_state as state_mod

    fake_state = SimpleNamespace(
        is_busy=lambda: False,
        status=SimpleNamespace(value="disabled"),
        to_dict=lambda: {"status": "disabled", "phase": None, "error": None, "ready": False, "busy": False},
    )
    monkeypatch.setattr(state_mod, "embedding_state", fake_state)


def _make_request(body: dict[str, Any]) -> object:
    """Minimal Starlette-compatible Request stand-in with a JSON body."""

    async def _json() -> dict:
        return body

    return SimpleNamespace(headers={}, cookies={}, json=_json)


def test_put_with_disabled_skips_preflight_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled=False`` is the cheap-and-safe path — the preflight gate
    must not fire, even if every embedding extras group is missing on
    disk. The handler just persists the disabled flag and schedules the
    disabled-path apply, which doesn't touch sentence_transformers."""
    _stub_admin_ok(monkeypatch)
    _stub_embedding_state_idle(monkeypatch)
    monkeypatch.setattr(
        config_api,
        "_resolve_vectorstore",
        lambda body: _coro(body),
    )
    persist_calls: list[dict] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.persist_embedding_config",
        lambda body, _cfg: persist_calls.append(body),
    )
    apply_calls: list[Any] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.apply_embedding_config_in_background",
        lambda **kw: apply_calls.append(kw) or True,
    )

    fake_storage = SimpleNamespace()
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=fake_storage,
        conversation_storage=SimpleNamespace(
            list_profiles=_coro_factory([{"name": "admin"}]),
        ),
    )
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    handler = next(r.endpoint for r in routes if r.path == "/api/config/embedding" and "PUT" in r.methods)

    response = asyncio.run(handler(_make_request({"enabled": False})))
    assert response.status_code == 202, response.body
    assert len(persist_calls) == 1
    assert persist_calls[0] == {"enabled": False}
    assert len(apply_calls) == 1


def test_put_with_enabled_returns_409_when_extras_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core regression check for the post-setup enable flow: when the
    user picked an embedding provider whose pip extras aren't installed,
    the handler must surface a 409 with the structured payload the
    frontend install dialog consumes — NOT swallow the gap and let the
    apply step crash on the missing import."""
    _stub_admin_ok(monkeypatch)
    _stub_embedding_state_idle(monkeypatch)
    # Pretend nothing is installed yet so the preflight identifies BOTH
    # embedding.me5 (sentence-transformers) and vectorstore.qdrant
    # (qdrant_client) as missing.
    monkeypatch.setattr(
        "app.features.manifest.is_installed",
        lambda _key: False,
    )

    handler = _get_handler()
    body = {
        "enabled": True,
        "provider": "me5",
        "vectorstore": {"provider": "qdrant"},
    }
    response = asyncio.run(handler(_make_request(body)))
    assert response.status_code == 409, response.body
    payload = json.loads(response.body)
    assert payload["error"] == "FeatureNotInstalled"
    keys = [m["feature_key"] for m in payload["missing"]]
    assert "embedding.me5" in keys
    assert "vectorstore.qdrant" in keys
    # Each entry carries the pyproject extras group(s) and the
    # restart-required flag the dialog uses to decide whether to retry
    # the apply automatically or stop after install. Embedding providers
    # carry requires_restart=True because torch + sentence-transformers
    # can't hot-reload safely in the live process.
    me5_entry = next(m for m in payload["missing"] if m["feature_key"] == "embedding.me5")
    assert me5_entry["extras"] == ["embeddings-me5"]
    assert me5_entry["requires_restart_after_install"] is True


def test_put_with_enabled_proceeds_when_extras_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-suspenders: the preflight is purely a gate, not a
    side-effecting step. When every required feature is already
    importable, the handler must fall through to the same
    ``_resolve_vectorstore → persist → apply`` chain it always ran."""
    _stub_admin_ok(monkeypatch)
    _stub_embedding_state_idle(monkeypatch)
    monkeypatch.setattr(
        "app.features.manifest.is_installed",
        lambda _key: True,
    )
    monkeypatch.setattr(
        config_api,
        "_resolve_vectorstore",
        lambda body: _coro(body),
    )
    persist_calls: list[dict] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.persist_embedding_config",
        lambda body, _cfg: persist_calls.append(body),
    )
    apply_calls: list[Any] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.apply_embedding_config_in_background",
        lambda **kw: apply_calls.append(kw) or True,
    )

    fake_storage = SimpleNamespace()
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=fake_storage,
        conversation_storage=SimpleNamespace(
            list_profiles=_coro_factory([{"name": "admin"}]),
        ),
    )
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    handler = next(r.endpoint for r in routes if r.path == "/api/config/embedding" and "PUT" in r.methods)

    body = {
        "enabled": True,
        "provider": "me5",
        "vectorstore": {"provider": "qdrant", "qdrant": {"deployment_mode": "external"}},
    }
    response = asyncio.run(handler(_make_request(body)))
    assert response.status_code == 202, response.body
    assert len(persist_calls) == 1
    assert len(apply_calls) == 1


def test_put_with_defer_apply_persists_without_applying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``defer_apply`` flag is set by the Settings page after a
    feature install whose result.restart_required=True (sentence-
    transformers + torch). Without this gate, the live process would
    try to apply, fail with ModuleNotFoundError, and leave the state
    FAILED — and worse, the user would see the page still report
    "Disabled" because the persist-but-don't-apply path doesn't exist
    yet. Contract: persist runs, apply is skipped, response is 200
    with deferred=True so the frontend knows to show the restart
    banner instead of polling for status."""
    _stub_admin_ok(monkeypatch)
    _stub_embedding_state_idle(monkeypatch)
    monkeypatch.setattr(
        "app.features.manifest.is_installed",
        lambda _key: True,
    )
    monkeypatch.setattr(
        config_api,
        "_resolve_vectorstore",
        lambda body: _coro(body),
    )
    persist_calls: list[dict] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.persist_embedding_config",
        lambda body, _cfg: persist_calls.append(body),
    )
    apply_calls: list[Any] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.apply_embedding_config_in_background",
        lambda **kw: apply_calls.append(kw) or True,
    )

    fake_storage = SimpleNamespace()
    state = SimpleNamespace(
        storage_ready=True,
        config_storage=fake_storage,
        conversation_storage=SimpleNamespace(
            list_profiles=_coro_factory([{"name": "admin"}]),
        ),
    )
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    handler = next(r.endpoint for r in routes if r.path == "/api/config/embedding" and "PUT" in r.methods)

    body = {
        "enabled": True,
        "provider": "me5",
        "vectorstore": {"provider": "qdrant", "qdrant": {"deployment_mode": "external"}},
        "defer_apply": True,
    }
    response = asyncio.run(handler(_make_request(body)))
    assert response.status_code == 200, response.body
    payload = json.loads(response.body)
    assert payload["deferred"] is True
    assert payload["success"] is True
    # The whole point of the flag — persist runs, apply does not.
    assert len(persist_calls) == 1
    assert persist_calls[0]["enabled"] is True
    assert apply_calls == []


def test_put_without_defer_apply_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression check: the existing apply path is reached when
    ``defer_apply`` is missing or false. The wizard's hot-loadable
    install (vectorstore-only, restart_required=False) and any
    subsequent re-apply after restart both depend on this path."""
    _stub_admin_ok(monkeypatch)
    _stub_embedding_state_idle(monkeypatch)
    monkeypatch.setattr(
        "app.features.manifest.is_installed",
        lambda _key: True,
    )
    monkeypatch.setattr(
        config_api,
        "_resolve_vectorstore",
        lambda body: _coro(body),
    )
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.persist_embedding_config",
        lambda _body, _cfg: None,
    )
    apply_calls: list[Any] = []
    monkeypatch.setattr(
        "app.lib.embedding_lifecycle.apply_embedding_config_in_background",
        lambda **kw: apply_calls.append(kw) or True,
    )

    state = SimpleNamespace(
        storage_ready=True,
        config_storage=SimpleNamespace(),
        conversation_storage=SimpleNamespace(
            list_profiles=_coro_factory([{"name": "admin"}]),
        ),
    )
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    handler = next(r.endpoint for r in routes if r.path == "/api/config/embedding" and "PUT" in r.methods)

    body = {
        "enabled": True,
        "provider": "me5",
        "vectorstore": {"provider": "qdrant", "qdrant": {"deployment_mode": "external"}},
        # No defer_apply — exercises the existing path.
    }
    response = asyncio.run(handler(_make_request(body)))
    assert response.status_code == 202, response.body
    assert len(apply_calls) == 1


def test_embedding_feature_keys_helper() -> None:
    """The helper that drives the preflight must always include BOTH
    the embedding provider and the vector store provider keys when
    embedding is enabled — the wizard's setup-payload analogue does the
    same, and the apply step reaches for both."""
    out = config_api._embedding_feature_keys(
        {
            "enabled": True,
            "provider": "gemma",
            "vectorstore": {"provider": "chroma"},
        }
    )
    assert out == ["embedding.gemma", "vectorstore.chroma"]

    # Defaults — empty provider falls back to me5; empty vectorstore
    # provider falls back to qdrant. Matches what the wizard payload
    # mapper does.
    out = config_api._embedding_feature_keys({"enabled": True})
    assert out == ["embedding.me5", "vectorstore.qdrant"]

    # Disabled → no preflight at all.
    out = config_api._embedding_feature_keys({"enabled": False})
    assert out == []


# ── helpers ───────────────────────────────────────────────────────────


async def _coro(value):
    return value


def _coro_factory(value):
    async def _f():
        return value

    return _f
