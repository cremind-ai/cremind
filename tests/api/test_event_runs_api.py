"""REST API for event runs: list (filters + usage rollup), get, delete, profile scoping."""

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
from app.storage.event_run_storage import EventRunStorage  # noqa: E402
from app.storage.usage_storage import UsageStorage  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.api.event_runs import get_event_run_routes  # noqa: E402

_TABLES = ("profiles", "channels", "conversations", "messages", "usage_records", "event_runs")


def _handler(path: str, method: str) -> Callable:
    for route in get_event_run_routes():
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="p1", path_params=None, query_params=None):
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        query_params=query_params or {},
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "api.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('pid','p1',0,0)"))
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('pid2','p2',0,0)"))

    cs = ConversationStorage(provider); cs._initialized = True
    ers = EventRunStorage(provider)
    us = UsageStorage(provider)

    import app.api.event_runs as api_mod
    import app.storage as storage_pkg
    monkeypatch.setattr(api_mod, "get_event_run_storage", lambda *a, **k: ers)
    monkeypatch.setattr(api_mod, "get_usage_storage", lambda *a, **k: us)
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    monkeypatch.setattr(storage_pkg, "get_event_run_storage", lambda *a, **k: ers)
    return cs, ers, us


async def _seed_run(cs, ers, us, profile="p1", status="completed", sub="s1", tokens=15):
    conv = await cs.create_conversation(profile=profile, title="run", kind="event_run")
    created = await ers.create(profile=profile, source_kind="schedule", subscription_id=sub,
                               conversation_id=conv["id"], label="L", action="a")
    rid = created["run"]["id"]
    await ers.update_status(rid, status=status, mark_finished=(status != "pending"))
    await us.add_usage_records(conv["id"], profile,
        [{"source_kind": "reasoning", "input_tokens": tokens - 5, "output_tokens": 5}],
        message_id=None, event_run_id=rid)
    return rid, conv["id"]


def test_list_get_delete_and_scoping(tmp_path: Path, monkeypatch) -> None:
    cs, ers, us = _setup(tmp_path, monkeypatch)
    list_h = _handler("/api/event-runs", "GET")
    get_h = _handler("/api/event-runs/{run_id}", "GET")
    del_h = _handler("/api/event-runs/{run_id}", "DELETE")

    async def run():
        rid, conv = await _seed_run(cs, ers, us, profile="p1", sub="s1")
        await _seed_run(cs, ers, us, profile="p1", status="pending", sub="s1")
        await _seed_run(cs, ers, us, profile="p2", sub="s9")  # other profile

        # list scoped to caller profile
        resp = _body(await list_h(_req("p1")))
        assert resp["total"] == 2
        first = resp["runs"][0]
        assert set(first["usage"].keys()) >= {"total_tokens", "total_usd", "request_count"}

        # status filter
        pend = _body(await list_h(_req("p1", query_params={"status": "pending"})))
        assert pend["total"] == 1 and pend["runs"][0]["status"] == "pending"

        # get with usage rollup
        one = _body(await get_h(_req("p1", path_params={"run_id": rid})))
        assert one["run"]["id"] == rid
        assert one["run"]["usage"]["total_tokens"] == 15

        # 404 across profile boundary
        r2 = await get_h(_req("p2", path_params={"run_id": rid}))
        assert r2.status_code == 404

        # delete removes the run + its conversation; usage survives
        before = (await us.totals(profile="p1"))["total_tokens"]
        ok = await del_h(_req("p1", path_params={"run_id": rid}))
        assert _body(ok)["ok"] is True
        assert await ers.get(rid) is None
        assert await cs.get_conversation(conv) is None
        after = (await us.totals(profile="p1"))["total_tokens"]
        assert after == before, "usage lost on run delete"

    asyncio.run(run())


def test_list_requires_auth(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    list_h = _handler("/api/event-runs", "GET")
    unauth = SimpleNamespace(user=SimpleNamespace(is_authenticated=False, username=""))
    resp = asyncio.run(list_h(unauth))
    assert resp.status_code == 401
