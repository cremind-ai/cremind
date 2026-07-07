"""PATCH /api/skill-events/{id}: action edit, declared-trigger validation, ownership.

Drives the route endpoint directly with a fake Request, backed by a real
``EventSubscriptionStorage`` on a temp SQLite DB. The skill's declared events
(used to validate a changed trigger) are stubbed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402
import app.api.events as ev_api  # noqa: E402
import app.tools.builtin.register_skill_event as RSE  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "skill_event_subscriptions")


def _handler(path: str, method: str) -> Callable:
    for route in ev_api.get_event_routes():
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="p1", path_params=None, body=None):
    async def _json():
        return body if body is not None else {}
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        json=_json,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def _setup(tmp_path: Path, monkeypatch, *, declared=("morning", "evening")):
    provider = SqliteDatabaseProvider(str(tmp_path / "api.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for pid, name in (("pid", "p1"), ("pid2", "p2")):
            c.execute(text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES (:id, :n, 0, 0)"), {"id": pid, "n": name})
        c.execute(text(
            "INSERT INTO conversations (id, profile, title, created_at, updated_at) "
            "VALUES ('c1', 'p1', 't', 0, 0)"))

    store = EventSubscriptionStorage(provider)
    monkeypatch.setattr(ev_api, "get_event_subscription_storage", lambda *a, **k: store)
    monkeypatch.setattr(ev_api, "publish_skill_events_admin_changed", lambda *a, **k: None)
    monkeypatch.setattr(ev_api, "_resolve_skill_source", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(RSE, "_read_events_metadata", lambda *a, **k: [{"name": n} for n in declared])
    return store


def _seed_sub(store, *, profile="p1"):
    return store.insert(
        conversation_id="c1", profile=profile, skill_name="daily-brief",
        event_type="morning", action="summarize",
    )


def test_update_action_only(tmp_path: Path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    row = _seed_sub(store)
    handler = _handler("/api/skill-events/{id}", "PATCH")

    resp = asyncio.run(handler(_req(path_params={"id": row["id"]}, body={"action": "new action"})))
    out = _body(resp)
    assert out["action"] == "new action"
    assert out["event_type"] == "morning"  # unchanged
    assert store.get(row["id"])["action"] == "new action"


def test_update_valid_trigger(tmp_path: Path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch, declared=("morning", "evening"))
    row = _seed_sub(store)
    handler = _handler("/api/skill-events/{id}", "PATCH")

    resp = asyncio.run(handler(_req(path_params={"id": row["id"]}, body={"event_type": "evening"})))
    out = _body(resp)
    assert out["event_type"] == "evening"
    assert store.get(row["id"])["event_type"] == "evening"


def test_update_rejects_undeclared_trigger(tmp_path: Path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch, declared=("morning",))
    row = _seed_sub(store)
    handler = _handler("/api/skill-events/{id}", "PATCH")

    resp = asyncio.run(handler(_req(path_params={"id": row["id"]}, body={"event_type": "bogus"})))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_trigger"
    assert store.get(row["id"])["event_type"] == "morning"  # unchanged


def test_update_rejects_empty_action(tmp_path: Path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    row = _seed_sub(store)
    handler = _handler("/api/skill-events/{id}", "PATCH")
    resp = asyncio.run(handler(_req(path_params={"id": row["id"]}, body={"action": ""})))
    assert resp.status_code == 400


def test_update_forbidden_other_profile(tmp_path: Path, monkeypatch) -> None:
    store = _setup(tmp_path, monkeypatch)
    row = _seed_sub(store, profile="p2")
    handler = _handler("/api/skill-events/{id}", "PATCH")
    resp = asyncio.run(handler(_req(username="p1", path_params={"id": row["id"]}, body={"action": "x"})))
    assert resp.status_code == 403
