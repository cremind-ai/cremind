"""GET /api/conversations/{id}/memory: admin reads every agent's memory.

The room's admin already sees every member agent's reasoning trace, so it may
also open a member seat's memory panel. The bypass alone is not enough, though:
the panel's three profile-scoped reads (the memory config, the long-term list,
the compaction threshold) used the VIEWER's profile, so an admin would have been
shown the seat's running summary sitting beside the ADMIN's own long-term
memories and the admin's own context budget — one panel stitched out of two
different agents' minds. They must all resolve against the seat's owner.

Members are unchanged: own conversation only. Compacting a seat stays refused
outright — that writes, and a seat is only ever written through the group.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.api.conversations import get_conversation_routes  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "usage_records", "event_runs")

_MEMORY = "/api/conversations/{conversation_id}/memory"
_TRIGGER = "/api/conversations/{conversation_id}/memory/trigger"


class _Req:
    def __init__(self, username, conversation_id):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.path_params = {"conversation_id": conversation_id}
        self.method = "GET"

    async def json(self):
        return {}


def _body(resp) -> dict:
    return json.loads(resp.body)


def _storage(tmp_path: Path) -> ConversationStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "memory.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p1','member',0,0), ('p2','admin',0,0)"
        ))
    cs = ConversationStorage(provider)
    cs._initialized = True
    return cs


def _scoped_calls(monkeypatch) -> dict[str, list[str]]:
    """Record the profile each of the three per-profile reads is made with."""
    seen: dict[str, list[str]] = {"config": [], "long_term": [], "usage": []}

    import app.api.conversations as conv_api
    import app.agent.compaction as compaction
    import app.agent.memory_vectorstore as memory_vectorstore
    import app.events.runner as runner

    def fake_config(profile):
        seen["config"].append(profile)
        return SimpleNamespace(enabled=True)

    def fake_list_long_term(*, agent, profile, limit=50):
        seen["long_term"].append(profile)
        return [{"content": f"fact for {profile}", "created_at": 0}]

    async def fake_usage(*, conversation_id, profile, conversation_storage, history=None):
        seen["usage"].append(profile)
        return {"current_tokens": 10, "threshold": 100, "context_window": 1000}

    monkeypatch.setattr(conv_api, "resolve_memory_config", fake_config)
    monkeypatch.setattr(conv_api.BaseConfig, "is_embedding_enabled", lambda: True)
    monkeypatch.setattr(conv_api.embedding_state, "is_ready", lambda: True)
    monkeypatch.setattr(memory_vectorstore, "list_long_term", fake_list_long_term)
    monkeypatch.setattr(runner, "get_cremind_agent", lambda: object())
    monkeypatch.setattr(compaction, "context_usage", fake_usage)
    return seen


def _handler(cs, path: str, method: str):
    for route in get_conversation_routes(cs):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _seat(cs) -> str:
    async def _create():
        conv = await cs.create_conversation(
            profile="member", title="seat", kind="group_chat",
            context_id="group:g-mem:member",
        )
        return conv["id"]

    return asyncio.run(_create())


def test_admin_reads_a_seat_scoped_to_its_owner(tmp_path, monkeypatch):
    cs = _storage(tmp_path)
    seen = _scoped_calls(monkeypatch)
    cid = _seat(cs)

    resp = asyncio.run(_handler(cs, _MEMORY, "GET")(_Req("admin", cid)))

    assert resp.status_code == 200
    # Every profile-scoped read used the seat's owner, never the viewer.
    assert seen == {"config": ["member"], "long_term": ["member"], "usage": ["member"]}
    assert _body(resp)["long_term"] == [{"content": "fact for member", "created_at": 0}]


def test_owner_reads_its_own_seat(tmp_path, monkeypatch):
    cs = _storage(tmp_path)
    seen = _scoped_calls(monkeypatch)
    cid = _seat(cs)

    resp = asyncio.run(_handler(cs, _MEMORY, "GET")(_Req("member", cid)))

    assert resp.status_code == 200
    assert seen["usage"] == ["member"]


def test_another_member_is_refused(tmp_path, monkeypatch):
    cs = _storage(tmp_path)
    seen = _scoped_calls(monkeypatch)
    cid = _seat(cs)

    resp = asyncio.run(_handler(cs, _MEMORY, "GET")(_Req("intruder", cid)))

    assert resp.status_code == 403
    # Refused before any memory was touched.
    assert seen == {"config": [], "long_term": [], "usage": []}


def test_compacting_a_seat_is_still_refused(tmp_path, monkeypatch):
    cs = _storage(tmp_path)
    _scoped_calls(monkeypatch)
    cid = _seat(cs)

    owner = asyncio.run(_handler(cs, _TRIGGER, "POST")(_Req("member", cid)))
    admin = asyncio.run(_handler(cs, _TRIGGER, "POST")(_Req("admin", cid)))

    assert owner.status_code == 403
    assert _body(owner)["error"] == "Group chat conversation"
    assert admin.status_code == 403
