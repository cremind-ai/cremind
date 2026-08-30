"""API: POST /messages while a turn is already running.

The handler now has a fork. Idle conversations behave exactly as before — this
file pins that too, because the injected path is only safe if the ordinary one
is untouched. A busy conversation instead hands the message to the running turn
and answers with the LIVE run's id, so a client's Stop still targets the right
run and no second run is implied.
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
from app.events.user_message_delivery import ParkOutcome  # noqa: E402

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
    enqueues: list = []

    async def fake_enqueue(**kwargs):
        captured.update(kwargs)
        enqueues.append(kwargs)

    import app.api.conversations as conv_api
    monkeypatch.setattr(conv_api.event_queue, "enqueue_user_message", fake_enqueue)
    monkeypatch.setattr(conv_api, "publish_conversations_changed", lambda *a, **k: None)

    park_calls: list = []
    outcome: dict = {"value": None}

    async def fake_park(**kwargs):
        park_calls.append(kwargs)
        return outcome["value"]

    monkeypatch.setattr(
        conv_api.user_message_delivery, "try_park_user_message", fake_park,
    )
    return cs, captured, enqueues, park_calls, outcome


def _post(cs, body, *, kind="chat"):
    async def _run():
        conv = await cs.create_conversation(profile="p1", title="c", kind=kind)
        handler = _handler("/api/conversations/{conversation_id}/messages", "POST")
        return await handler(
            _Req(path_params={"conversation_id": conv["id"]}, body=body),
        )

    return asyncio.run(_run())


def test_an_idle_conversation_still_queues_a_turn(tmp_path, monkeypatch):
    cs, captured, enqueues, park_calls, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = None                      # idle

    resp = _post(cs, {"text": "hello"})

    assert resp.status_code == 202
    assert _body(resp)["delivery"] == "queued"
    assert len(enqueues) == 1
    # The pre-existing contract for a normal turn is unchanged.
    assert captured["push_user_message"] is True
    assert captured["existing_user_message_id"] is None
    assert captured["query"] == "hello"


def test_a_busy_conversation_injects_and_reports_the_live_run(tmp_path, monkeypatch):
    cs, _captured, enqueues, park_calls, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(
        injected=True, message_id="m1", run_id="msg:live:1",
    )

    resp = _post(cs, {"text": "actually, staging"})

    body = _body(resp)
    assert resp.status_code == 202
    assert body["delivery"] == "injected"
    assert body["message_id"] == "m1"
    # The LIVE run, so Stop cancels the turn the message went into.
    assert body["run_id"] == "msg:live:1"
    assert enqueues == []                         # no second turn


def test_the_park_receives_the_turn_settings(tmp_path, monkeypatch):
    cs, _captured, _enq, park_calls, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(injected=True, message_id="m1", run_id="r1")

    _post(cs, {"text": "hi", "mode": "instant"})

    call = park_calls[0]
    assert call["query"] == "hi"
    assert call["mode"] == "instant"
    assert call["profile"] == "p1"
    assert call["user_message_metadata"]["mode"] == "instant"


def test_losing_the_race_runs_the_turn_without_persisting_twice(
    tmp_path, monkeypatch,
):
    cs, captured, enqueues, _park, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(injected=False, message_id="m9")

    resp = _post(cs, {"text": "too late"})

    assert _body(resp)["delivery"] == "queued"
    assert len(enqueues) == 1
    assert captured["push_user_message"] is False
    assert captured["existing_user_message_id"] == "m9"


def test_a_race_lost_park_still_tells_the_sender_the_row_id(tmp_path, monkeypatch):
    """The row was persisted AND its frame published before this response. The
    sender needs the id to recognise its own optimistic bubble, or the frame
    arrives unrecognised and the message renders twice."""
    cs, _captured, _enq, _park, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(injected=False, message_id="m9")

    assert _body(_post(cs, {"text": "too late"}))["message_id"] == "m9"


def test_an_ordinary_queued_send_reports_no_row_id(tmp_path, monkeypatch):
    """Nothing was persisted before responding — the runner will do it."""
    cs, _captured, _enq, _park, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = None

    assert _body(_post(cs, {"text": "hello"}))["message_id"] is None


def test_a_plan_accept_never_parks(tmp_path, monkeypatch):
    """It starts the execute phase; it is not a remark to fold into a live turn."""
    cs, captured, enqueues, park_calls, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(injected=True, message_id="m1", run_id="r1")

    resp = _post(cs, {"text": "go", "mode": "plan", "plan_action": "accept"})

    assert park_calls == []
    assert _body(resp)["delivery"] == "queued"
    assert len(enqueues) == 1
    assert captured["plan_action"] == "accept"


def test_an_event_run_reply_can_be_injected(tmp_path, monkeypatch):
    cs, _captured, enqueues, park_calls, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(injected=True, message_id="m1", run_id="r1")

    resp = _post(cs, {"text": "the answer is 4"}, kind="event_run")

    assert _body(resp)["delivery"] == "injected"
    assert park_calls[0]["event_run"] is True
    assert enqueues == []


def test_an_empty_message_is_still_rejected_before_any_park(tmp_path, monkeypatch):
    cs, _captured, enqueues, park_calls, outcome = _setup(tmp_path, monkeypatch)
    outcome["value"] = ParkOutcome(injected=True, message_id="m1", run_id="r1")

    resp = _post(cs, {"text": "   "})

    assert resp.status_code == 400
    assert park_calls == []
    assert enqueues == []
