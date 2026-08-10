"""The deadline sweep that stops a never-firing task from hanging a conversation.

The sweep's whole job is to be safe against the two races that surround it: a
task that fires between the scan and the claim, and a task whose run is already
in flight. Both must leave the real firing alone.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.events.task_timeout_manager import TaskTimeoutManager  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations",
    "skill_event_subscriptions", "file_watcher_subscriptions",
)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "timeouts.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('pid','p1',0,0)"
        ))
        c.execute(text(
            "INSERT INTO conversations (id, profile, kind, title, "
            "compaction_watermark, created_at, updated_at) "
            "VALUES ('c1','p1','chat','Chat',-1,0,0)"
        ))

    subs = EventSubscriptionStorage(provider)
    fws = FileWatcherSubscriptionStorage(provider)

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_event_subscription_storage", lambda *a, **k: subs)
    monkeypatch.setattr(storage_pkg, "get_file_watcher_storage", lambda *a, **k: fws)

    reported = []

    async def _deliver(source_kind, sub):
        reported.append((source_kind, sub["id"]))
        return "delivered"

    monkeypatch.setattr(
        "app.events.event_task_delivery.deliver_timeout", _deliver, raising=False,
    )
    return subs, fws, reported


def _task(subs, *, timeout_at, task=True):
    return subs.insert(
        conversation_id="c1", profile="p1", skill_name="imap-email",
        event_type="new-mail", action="report the reply",
        task=task, timeout_at=timeout_at,
    )


def test_overdue_task_is_expired_and_reported(tmp_path, monkeypatch):
    subs, _, reported = _setup(tmp_path, monkeypatch)
    sub = _task(subs, timeout_at=time.time() - 60)

    assert asyncio.run(TaskTimeoutManager().sweep_once()) == 1
    assert reported == [("skill_event", sub["id"])]
    assert subs.get(sub["id"])["task_status"] == "timed_out"


def test_a_task_already_running_is_left_alone(tmp_path, monkeypatch):
    """Its answer is on the way — expiring it now would race the real result."""
    subs, _, reported = _setup(tmp_path, monkeypatch)
    sub = _task(subs, timeout_at=time.time() - 60)
    subs.claim_task_fire(sub["id"])

    assert asyncio.run(TaskTimeoutManager().sweep_once()) == 0
    assert reported == []
    assert subs.get(sub["id"])["task_status"] == "triggered"


def test_a_task_that_fires_between_scan_and_claim_is_left_alone(tmp_path, monkeypatch):
    """The claim, not the SELECT, is what decides — so this race is safe."""
    subs, _, reported = _setup(tmp_path, monkeypatch)
    sub = _task(subs, timeout_at=time.time() - 60)

    real = subs.claim_task_timeout

    def _fire_first(sub_id):
        subs.claim_task_fire(sub_id)     # a trigger lands right now
        return real(sub_id)

    monkeypatch.setattr(subs, "claim_task_timeout", _fire_first)

    assert asyncio.run(TaskTimeoutManager().sweep_once()) == 0
    assert reported == []
    assert subs.get(sub["id"])["task_status"] == "triggered"


def test_a_task_expires_only_once(tmp_path, monkeypatch):
    subs, _, reported = _setup(tmp_path, monkeypatch)
    _task(subs, timeout_at=time.time() - 60)

    manager = TaskTimeoutManager()
    assert asyncio.run(manager.sweep_once()) == 1
    assert asyncio.run(manager.sweep_once()) == 0
    assert len(reported) == 1


def test_deadlines_in_the_future_and_standing_rules_are_ignored(tmp_path, monkeypatch):
    subs, _, reported = _setup(tmp_path, monkeypatch)
    _task(subs, timeout_at=time.time() + 3600)
    _task(subs, timeout_at=None)                       # waits indefinitely
    _task(subs, timeout_at=time.time() - 60, task=False)   # standing rule

    assert asyncio.run(TaskTimeoutManager().sweep_once()) == 0
    assert reported == []


def test_both_families_are_swept(tmp_path, monkeypatch):
    subs, fws, reported = _setup(tmp_path, monkeypatch)
    skill = _task(subs, timeout_at=time.time() - 60)
    watcher = fws.insert(
        conversation_id="c1", profile="p1", name="err-log", root_path="/tmp",
        recursive=True, target_kind="file", event_types="created", extensions="",
        action="report the error", task=True, timeout_at=time.time() - 60,
    )

    assert asyncio.run(TaskTimeoutManager().sweep_once()) == 2
    assert sorted(reported) == sorted([
        ("skill_event", skill["id"]), ("file_watcher", watcher["id"]),
    ])
