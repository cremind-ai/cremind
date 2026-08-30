"""API: a group-chat seat is read-only through the conversations endpoints.

A seat is one member's private view of a room. Writing into it directly would
show that agent words the room's timeline never recorded — and the other members
never saw — so every mutating conversation route refuses a ``kind="group_chat"``
row and points the caller at the group endpoints instead.

The important half of each assertion is the second one: the refusal has to come
BEFORE anything is parked, queued or torn down, or a "rejected" request would
still have started a turn.
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
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.api.conversations import get_conversation_routes  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "usage_records", "event_runs")


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
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))

    cs = ConversationStorage(provider)
    cs._initialized = True

    enqueues: list = []
    park_calls: list = []
    deleted: list = []

    async def fake_enqueue(**kwargs):
        enqueues.append(kwargs)

    async def fake_park(**kwargs):
        park_calls.append(kwargs)
        return None

    async def fake_cleanup(_storage, conversation_id):
        deleted.append(conversation_id)

    import app.api.conversations as conv_api
    import app.reset._conversations as reset_conversations

    monkeypatch.setattr(conv_api.event_queue, "enqueue_user_message", fake_enqueue)
    monkeypatch.setattr(conv_api, "publish_conversations_changed", lambda *a, **k: None)
    monkeypatch.setattr(
        conv_api.user_message_delivery, "try_park_user_message", fake_park,
    )
    monkeypatch.setattr(
        reset_conversations, "cleanup_conversation_dependents", fake_cleanup,
    )
    return cs, enqueues, park_calls, deleted


def _handler(cs, path: str, method: str) -> Callable:
    for route in get_conversation_routes(cs):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _call(cs, path, method, *, body=None, kind="group_chat", extra_params=None):
    async def _run():
        conv = await cs.create_conversation(profile="p1", title="seat", kind=kind)
        handler = _handler(cs, path, method)
        params = {"conversation_id": conv["id"], **(extra_params or {})}
        return await handler(_Req(path_params=params, body=body, method=method))

    return asyncio.run(_run())


_MESSAGES = "/api/conversations/{conversation_id}/messages"
_DETAIL = "/api/conversations/{conversation_id}"
_MEMORY_TRIGGER = "/api/conversations/{conversation_id}/memory/trigger"
_PLAN_CANCEL = "/api/conversations/{conversation_id}/plan/cancel"


def test_posting_into_a_seat_is_refused(tmp_path, monkeypatch):
    cs, enqueues, park_calls, _deleted = _setup(tmp_path, monkeypatch)

    resp = _call(cs, _MESSAGES, "POST", body={"text": "hello"})

    assert resp.status_code == 403
    assert _body(resp)["error"] == "Group chat conversation"
    assert "/api/group-chats/" in _body(resp)["message"]
    # Nothing was parked and nothing was queued: the guard runs before both.
    assert park_calls == []
    assert enqueues == []


def test_renaming_a_seat_is_refused(tmp_path, monkeypatch):
    cs, _enq, _park, _deleted = _setup(tmp_path, monkeypatch)

    resp = _call(cs, _DETAIL, "PUT", body={"title": "mine now"})

    assert resp.status_code == 403
    assert _body(resp)["error"] == "Group chat conversation"


def test_deleting_a_seat_is_refused_and_tears_nothing_down(tmp_path, monkeypatch):
    cs, _enq, _park, deleted = _setup(tmp_path, monkeypatch)

    resp = _call(cs, _DETAIL, "DELETE")

    assert resp.status_code == 403
    assert deleted == []


def test_triggering_memory_on_a_seat_is_refused(tmp_path, monkeypatch):
    cs, _enq, _park, _deleted = _setup(tmp_path, monkeypatch)

    resp = _call(cs, _MEMORY_TRIGGER, "POST", body={})

    assert resp.status_code == 403
    assert _body(resp)["error"] == "Group chat conversation"


def test_cancelling_a_plan_on_a_seat_is_refused(tmp_path, monkeypatch):
    cs, _enq, _park, _deleted = _setup(tmp_path, monkeypatch)

    resp = _call(cs, _PLAN_CANCEL, "POST", body={})

    assert resp.status_code == 403


def test_an_ordinary_conversation_is_untouched(tmp_path, monkeypatch):
    """The guard keys on ``kind`` alone — a normal chat still queues its turn."""
    cs, enqueues, park_calls, _deleted = _setup(tmp_path, monkeypatch)

    resp = _call(cs, _MESSAGES, "POST", body={"text": "hello"}, kind="chat")

    assert resp.status_code == 202
    assert len(enqueues) == 1
    assert len(park_calls) == 1
