"""POST /api/skill-events/{id}/simulate: what it warns you it is about to do.

Simulate is not a dry run, in two ways a user cannot see from the button. The
file goes into the watched folder, so the subscription id in the URL only picks
the folder — every subscription for that (skill, event_type) fires. And each of
those runs now reports its result into the conversation that registered it, so
pressing this sends real messages, possibly into a room or a platform group.

The count has to be the truth, which means applying the same filter the fan-out
applies: a paused rule and a spent one-shot do not run.
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

import app.api.events as ev_api  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402

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


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "sim.db"))
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
    monkeypatch.setattr(ev_api, "_resolve_skill_source", lambda *a, **k: str(tmp_path))
    return store, eng


def _sub(store, eng, *, profile="p1", task=False, status=None, paused=False):
    row = store.insert(
        conversation_id="c1", profile=profile, skill_name="daily-brief",
        event_type="morning", action="summarize", task=task,
    )
    if status:
        # Straight to the column: the storage only exposes the atomic claim
        # transitions, and this is a fixture, not the state machine under test.
        with eng.begin() as c:
            c.execute(
                text("UPDATE skill_event_subscriptions SET task_status = :s WHERE id = :i"),
                {"s": status, "i": row["id"]},
            )
    if paused:
        store.update_fields(row["id"], paused=True)
    return store.get(row["id"])


def _fire(store, row, tmp_path):
    handler = _handler("/api/skill-events/{id}/simulate", "POST")
    return _body(asyncio.run(handler(_req(
        path_params={"id": row["id"]}, body={"content": "# a mail"},
    ))))


def test_it_warns_that_every_live_subscription_fires_and_posts(tmp_path, monkeypatch):
    store, eng = _setup(tmp_path, monkeypatch)
    row = _sub(store, eng)
    _sub(store, eng)                                   # a live sibling
    _sub(store, eng, paused=True)                      # skipped by the fan-out
    _sub(store, eng, task=True, status="completed")    # a spent one-shot, also skipped
    _sub(store, eng, profile="p2")                     # another tenant entirely

    out = _fire(store, row, tmp_path)

    assert out["ok"] is True
    assert len(out["warnings"]) == 1
    warning = out["warnings"][0]
    assert "EVERY active subscription" in warning
    assert "(2)" in warning, "paused and spent rules do not fire"
    assert "reports its result into the conversation" in warning
    # The old single-string field is gone; the CLI and the dialog read the list.
    assert "task_warning" not in out


def test_a_live_one_shot_is_warned_that_it_will_be_spent(tmp_path, monkeypatch):
    """The dispatcher cannot tell a simulated trigger from a real one."""
    store, eng = _setup(tmp_path, monkeypatch)
    row = _sub(store, eng, task=True, status="active")

    out = _fire(store, row, tmp_path)

    assert len(out["warnings"]) == 2
    assert "consumes its single firing" in out["warnings"][1]


def test_a_finished_one_shot_has_nothing_left_to_consume(tmp_path, monkeypatch):
    store, eng = _setup(tmp_path, monkeypatch)
    row = _sub(store, eng, task=True, status="completed")

    out = _fire(store, row, tmp_path)

    assert len(out["warnings"]) == 1
    assert all("consumes" not in w for w in out["warnings"])


def test_another_profiles_subscription_is_not_simulatable(tmp_path, monkeypatch):
    store, eng = _setup(tmp_path, monkeypatch)
    row = _sub(store, eng, profile="p2")
    handler = _handler("/api/skill-events/{id}/simulate", "POST")

    resp = asyncio.run(handler(_req(
        username="p1", path_params={"id": row["id"]}, body={"content": "x"},
    )))
    assert resp.status_code == 403
