"""PATCH /api/file-watchers/{id}: field validation, ownership, and re-arm.

Drives the route endpoint directly (no server) with a fake Request, backed by
a real ``FileWatcherSubscriptionStorage`` on a temp SQLite DB. A fake manager
records disarm/arm so we can assert the observer is re-armed on edit.
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
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage  # noqa: E402
import app.api.file_watchers as fw_api  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "file_watcher_subscriptions")


class _FakeManager:
    def __init__(self):
        self.disarmed = []
        self.armed = []

    def disarm(self, row):
        self.disarmed.append(row)

    def arm(self, row):
        self.armed.append(row)
        return True

    def is_armed(self, row):
        return True


def _handler(path: str, method: str) -> Callable:
    for route in fw_api.get_file_watcher_routes():
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


def _setup(tmp_path: Path, monkeypatch):
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

    store = FileWatcherSubscriptionStorage(provider)
    mgr = _FakeManager()
    monkeypatch.setattr(fw_api, "get_file_watcher_storage", lambda *a, **k: store)
    monkeypatch.setattr(fw_api, "get_file_watcher_manager", lambda *a, **k: mgr)
    monkeypatch.setattr(fw_api, "publish_file_watchers_admin_changed", lambda *a, **k: None)
    return store, mgr


def _seed_watcher(store, *, profile="p1"):
    return store.insert(
        conversation_id="c1", profile=profile, name="w", root_path="/tmp/w",
        recursive=True, target_kind="any", event_types="created,modified",
        extensions=".py", action="do a thing",
    )


def test_update_changes_fields_and_rearms(tmp_path: Path, monkeypatch) -> None:
    store, mgr = _setup(tmp_path, monkeypatch)
    row = _seed_watcher(store)
    handler = _handler("/api/file-watchers/{id}", "PATCH")

    resp = asyncio.run(handler(_req(
        path_params={"id": row["id"]},
        body={"triggers": ["modified"], "target_kind": "file",
              "recursive": False, "action": "do another thing"},
    )))
    out = _body(resp)
    assert out["event_types"] == "modified"
    assert out["target_kind"] == "file"
    assert out["recursive"] is False
    assert out["action"] == "do another thing"
    assert out["armed"] is True
    # persisted
    saved = store.get(row["id"])
    assert saved["event_types"] == "modified"
    # re-armed: old row disarmed, new row armed
    assert len(mgr.disarmed) == 1 and mgr.disarmed[0]["id"] == row["id"]
    assert len(mgr.armed) == 1 and mgr.armed[0]["event_types"] == "modified"


def test_update_action_only_does_not_disarm(tmp_path: Path, monkeypatch) -> None:
    # Editing only the action leaves the (root, recursive) key unchanged, so the
    # shared observer must NOT be torn down (arm stays idempotent, no disarm).
    store, mgr = _setup(tmp_path, monkeypatch)
    row = _seed_watcher(store)
    handler = _handler("/api/file-watchers/{id}", "PATCH")

    resp = asyncio.run(handler(_req(
        path_params={"id": row["id"]}, body={"action": "just a new action"})))
    out = _body(resp)
    assert out["action"] == "just a new action"
    assert out["root_path"] == "/tmp/w"  # unchanged
    assert len(mgr.armed) == 1          # re-armed (idempotent)
    assert mgr.disarmed == []           # old observer left alone


def test_update_rejects_invalid_trigger(tmp_path: Path, monkeypatch) -> None:
    store, mgr = _setup(tmp_path, monkeypatch)
    row = _seed_watcher(store)
    handler = _handler("/api/file-watchers/{id}", "PATCH")

    resp = asyncio.run(handler(_req(
        path_params={"id": row["id"]}, body={"triggers": ["exploded"]})))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "invalid_trigger"
    # nothing changed, nothing re-armed
    assert store.get(row["id"])["event_types"] == "created,modified"
    assert mgr.armed == []


def test_update_rejects_empty_action(tmp_path: Path, monkeypatch) -> None:
    store, mgr = _setup(tmp_path, monkeypatch)
    row = _seed_watcher(store)
    handler = _handler("/api/file-watchers/{id}", "PATCH")

    resp = asyncio.run(handler(_req(path_params={"id": row["id"]}, body={"action": "   "})))
    assert resp.status_code == 400
    assert mgr.armed == []


def test_update_forbidden_other_profile(tmp_path: Path, monkeypatch) -> None:
    store, mgr = _setup(tmp_path, monkeypatch)
    row = _seed_watcher(store, profile="p2")
    handler = _handler("/api/file-watchers/{id}", "PATCH")

    resp = asyncio.run(handler(_req(
        username="p1", path_params={"id": row["id"]}, body={"action": "x"})))
    assert resp.status_code == 403


def test_update_missing_watcher_404(tmp_path: Path, monkeypatch) -> None:
    store, mgr = _setup(tmp_path, monkeypatch)
    handler = _handler("/api/file-watchers/{id}", "PATCH")
    resp = asyncio.run(handler(_req(path_params={"id": "nope"}, body={"action": "x"})))
    assert resp.status_code == 404
