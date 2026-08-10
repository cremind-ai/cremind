"""Event-task claim semantics on the two subscription storages.

The claim is the whole correctness story for a one-shot: exactly one of
"a trigger fired it" / "its deadline passed" may win, no matter how many
callers race. These tests pin that, plus the immutability of task-ness.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.event_subscription_storage import EventSubscriptionStorage  # noqa: E402
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations",
    "skill_event_subscriptions", "file_watcher_subscriptions",
)


def _setup(tmp_path: Path):
    provider = SqliteDatabaseProvider(str(tmp_path / "claims.db"))
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
    return provider


def _skill_store(tmp_path):
    return EventSubscriptionStorage(_setup(tmp_path))


def _insert_skill(store, *, task=True, timeout_at=None):
    return store.insert(
        conversation_id="c1", profile="p1", skill_name="imap-email",
        event_type="new-mail", action="report the reply",
        task=task, timeout_at=timeout_at,
    )


def _insert_watcher(store, *, task=True, timeout_at=None):
    return store.insert(
        conversation_id="c1", profile="p1", name="err-log",
        root_path="/tmp/logs", recursive=True, target_kind="file",
        event_types="created", extensions=".log", action="report the error",
        task=task, timeout_at=timeout_at,
    )


# ── insert ──────────────────────────────────────────────────────────────────


def test_task_insert_starts_active(tmp_path):
    store = _skill_store(tmp_path)
    deadline = time.time() + 600
    row = _insert_skill(store, timeout_at=deadline)
    assert row["task"] is True
    assert row["task_status"] == "active"
    assert row["timeout_at"] == pytest.approx(deadline)
    assert row["completed_at"] is None
    assert store.get(row["id"])["task_status"] == "active"


def test_standing_insert_has_no_task_lifecycle(tmp_path):
    """A standing subscription must look exactly as it did before the feature."""
    store = _skill_store(tmp_path)
    row = _insert_skill(store, task=False, timeout_at=time.time() + 600)
    assert row["task"] is False
    assert row["task_status"] is None
    # A timeout on a non-task is meaningless and must not be persisted.
    assert row["timeout_at"] is None


# ── claim ───────────────────────────────────────────────────────────────────


def test_fire_claim_wins_exactly_once(tmp_path):
    """Ten concurrent triggers, one firing — the rest must be dropped."""
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store)["id"]
    wins = [store.claim_task_fire(sub_id) for _ in range(10)]
    assert wins.count(True) == 1
    assert wins[0] is True
    assert store.get(sub_id)["task_status"] == "triggered"


def test_timeout_and_fire_claims_are_mutually_exclusive(tmp_path):
    """Whichever of "it fired" / "it expired" lands first, the other is a no-op."""
    store = _skill_store(tmp_path)
    fired = _insert_skill(store)["id"]
    assert store.claim_task_fire(fired) is True
    assert store.claim_task_timeout(fired) is False   # run already in flight
    assert store.get(fired)["task_status"] == "triggered"

    expired = _insert_skill(store)["id"]
    assert store.claim_task_timeout(expired) is True
    assert store.claim_task_fire(expired) is False    # deadline already passed
    row = store.get(expired)
    assert row["task_status"] == "timed_out"
    assert row["completed_at"] is not None


def test_claims_never_touch_a_standing_subscription(tmp_path):
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store, task=False)["id"]
    assert store.claim_task_fire(sub_id) is False
    assert store.claim_task_timeout(sub_id) is False
    assert store.get(sub_id)["task_status"] is None


def test_revert_re_arms_a_claimed_task(tmp_path):
    """A claim whose run could not be created must not eat the one shot."""
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store)["id"]
    assert store.claim_task_fire(sub_id) is True
    assert store.revert_task_claim(sub_id) is True
    assert store.get(sub_id)["task_status"] == "active"
    assert store.claim_task_fire(sub_id) is True      # armed again


def test_terminate_only_from_triggered(tmp_path):
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store)["id"]
    # Cannot complete a task that never fired.
    assert store.set_task_status(sub_id, "completed") is False
    store.claim_task_fire(sub_id)
    assert store.set_task_status(sub_id, "completed") is True
    row = store.get(sub_id)
    assert row["task_status"] == "completed"
    assert row["completed_at"] is not None
    # And a second delivery cannot re-terminate it.
    assert store.set_task_status(sub_id, "completed") is False


# ── immutability + listings ─────────────────────────────────────────────────


def test_update_fields_cannot_change_task_ness(tmp_path):
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store, task=False)["id"]
    store.update_fields(sub_id, task=True, task_status="active", completed_at=1.0)
    row = store.get(sub_id)
    assert row["task"] is False
    assert row["task_status"] is None
    assert row["completed_at"] is None


def test_update_fields_may_move_the_deadline(tmp_path):
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store, timeout_at=time.time() + 60)["id"]
    later = time.time() + 7200
    store.update_fields(sub_id, timeout_at=later)
    assert store.get(sub_id)["timeout_at"] == pytest.approx(later)


def test_list_due_timeouts_only_returns_armed_overdue_tasks(tmp_path):
    store = _skill_store(tmp_path)
    past = time.time() - 60
    overdue = _insert_skill(store, timeout_at=past)["id"]
    _insert_skill(store, timeout_at=time.time() + 3600)      # not yet due
    _insert_skill(store, timeout_at=None)                    # waits forever
    _insert_skill(store, task=False, timeout_at=past)        # standing rule
    already_fired = _insert_skill(store, timeout_at=past)["id"]
    store.claim_task_fire(already_fired)

    due = [s["id"] for s in store.list_due_timeouts()]
    assert due == [overdue]


def test_paused_task_still_times_out(tmp_path):
    """The deadline is a promise to the waiting conversation, not to the watcher.

    A paused task cannot fire, so without this it would hang its conversation
    forever with no way for the assistant to notice.
    """
    store = _skill_store(tmp_path)
    sub_id = _insert_skill(store, timeout_at=time.time() - 5)["id"]
    store.update_fields(sub_id, paused=True)
    assert [s["id"] for s in store.list_due_timeouts()] == [sub_id]


def test_list_active_tasks_excludes_spent_ones(tmp_path):
    store = _skill_store(tmp_path)
    live = _insert_skill(store)["id"]
    spent = _insert_skill(store)["id"]
    store.claim_task_fire(spent)
    store.set_task_status(spent, "completed")
    assert [s["id"] for s in store.list_active_tasks(profile="p1")] == [live]


# ── the file-watcher storage behaves identically ────────────────────────────


def test_file_watcher_claims_mirror_skill_events(tmp_path):
    store = FileWatcherSubscriptionStorage(_setup(tmp_path))
    row = _insert_watcher(store, timeout_at=time.time() - 1)
    assert row["task"] is True and row["task_status"] == "active"
    assert [s["id"] for s in store.list_due_timeouts()] == [row["id"]]
    assert store.claim_task_fire(row["id"]) is True
    assert store.claim_task_fire(row["id"]) is False
    assert store.set_task_status(row["id"], "completed") is True
    assert store.get(row["id"])["task_status"] == "completed"


def test_file_watcher_standing_row_unchanged(tmp_path):
    store = FileWatcherSubscriptionStorage(_setup(tmp_path))
    row = _insert_watcher(store, task=False)
    assert row["task"] is False and row["task_status"] is None
    assert store.list_due_timeouts() == []
