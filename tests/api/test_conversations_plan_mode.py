"""API: POST /messages mode parsing + event-run coercion, and the run-free
POST /plan/cancel endpoint."""

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
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.api.conversations import get_conversation_routes  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "usage_records", "event_runs")


def _handler(path: str, method: str) -> Callable:
    for route in get_conversation_routes(_DummyStorage.instance):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


class _DummyStorage:
    instance: ConversationStorage = None  # type: ignore[assignment]


class _Req:
    def __init__(self, username="p1", path_params=None, body=None, method="POST"):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.path_params = path_params or {}
        self.method = method
        self._body = body or {}

    async def json(self):
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.body)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "api.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('pid','p1',0,0)"))

    cs = ConversationStorage(provider)
    cs._initialized = True
    _DummyStorage.instance = cs

    captured: dict = {}

    async def fake_enqueue(**kwargs):
        captured.update(kwargs)

    import app.api.conversations as conv_api
    monkeypatch.setattr(conv_api.event_queue, "enqueue_user_message", fake_enqueue)
    monkeypatch.setattr(conv_api, "publish_conversations_changed", lambda *a, **k: None)
    return cs, captured


def test_post_message_threads_plan_mode(tmp_path, monkeypatch):
    cs, captured = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        handler = _handler("/api/conversations/{conversation_id}/messages", "POST")
        resp = await handler(_Req(path_params={"conversation_id": conv["id"]},
                                  body={"text": "plan it", "mode": "plan"}))
        return resp

    resp = asyncio.run(_run())
    assert resp.status_code == 202
    assert captured["mode"] == "plan"


def test_post_message_absent_mode_derives_instant(tmp_path, monkeypatch):
    cs, captured = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        handler = _handler("/api/conversations/{conversation_id}/messages", "POST")
        await handler(_Req(path_params={"conversation_id": conv["id"]},
                           body={"text": "hi", "reasoning": False}))

    asyncio.run(_run())
    assert captured["mode"] == "instant"


def test_post_message_stamps_mode_metadata_for_plan(tmp_path, monkeypatch):
    cs, captured = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        handler = _handler("/api/conversations/{conversation_id}/messages", "POST")
        await handler(_Req(path_params={"conversation_id": conv["id"]},
                           body={"text": "plan it", "mode": "plan"}))

    asyncio.run(_run())
    meta = captured["user_message_metadata"]
    assert meta is not None and meta.get("mode") == "plan"


def test_post_message_reasoning_stamps_no_mode_metadata(tmp_path, monkeypatch):
    cs, captured = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        handler = _handler("/api/conversations/{conversation_id}/messages", "POST")
        await handler(_Req(path_params={"conversation_id": conv["id"]},
                           body={"text": "hi"}))

    asyncio.run(_run())
    # Default reasoning mode leaves user_message_metadata unset (no mode chip).
    assert captured.get("user_message_metadata") is None


def test_post_message_event_run_forces_reasoning(tmp_path, monkeypatch):
    cs, captured = _setup(tmp_path, monkeypatch)

    # Event-run conversations resume via get_event_run_storage; stub it away.
    import app.storage as storage_pkg
    monkeypatch.setattr(
        storage_pkg, "get_event_run_storage",
        lambda *a, **k: SimpleNamespace(get_by_conversation=_none),
        raising=False,
    )

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="run", kind="event_run")
        handler = _handler("/api/conversations/{conversation_id}/messages", "POST")
        await handler(_Req(path_params={"conversation_id": conv["id"]},
                           body={"text": "hi", "mode": "plan"}))

    asyncio.run(_run())
    # Plan mode is coerced off inside a hidden event-run conversation.
    assert captured["mode"] == "reasoning"


async def _none(*a, **k):
    return None


def test_plan_cancel_persists_marker(tmp_path, monkeypatch):
    cs, _ = _setup(tmp_path, monkeypatch)

    published: list = []

    class _Bus:
        async def publish(self, cid, etype, data):
            published.append((cid, etype, data))

    import app.api.conversations as conv_api
    monkeypatch.setattr(conv_api, "get_event_stream_bus", lambda: _Bus())

    async def _run():
        conv = await cs.create_conversation(profile="p1", title="c", kind="chat")
        handler = _handler("/api/conversations/{conversation_id}/plan/cancel", "POST")
        resp = await handler(_Req(path_params={"conversation_id": conv["id"]}))
        msgs = await cs.get_messages(conv["id"])
        return resp, msgs

    resp, msgs = asyncio.run(_run())
    assert resp.status_code == 200
    mid = _body(resp)["message_id"]
    assert mid
    # A visible user-role marker is persisted, tagged cancelled.
    marker = [m for m in msgs if m["id"] == mid][0]
    assert marker["role"] == "user"
    assert marker["metadata"]["plan_mode"]["stage"] == "cancelled"
    # And a plan_decision event was broadcast.
    assert any(etype == "plan_decision" and data["decision"] == "cancelled"
               for _, etype, data in published)
