"""Skill-event subscription storage: insert + editable-field patching.

Mirrors ``tests/storage/test_schedule_event_storage.py``. Locks in that
``update_fields`` edits only ``event_type`` / ``action`` and leaves
``skill_name`` and the identity columns alone.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.event_subscription_storage import EventSubscriptionStorage

_TABLES = ("profiles", "channels", "conversations", "skill_event_subscriptions")


def _make_store(tmp_path: Path) -> EventSubscriptionStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "se.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    return EventSubscriptionStorage(provider)


def _seed(store: EventSubscriptionStorage, *, profile="admin", conv="c1") -> None:
    from sqlalchemy import text

    now = time.time()
    with store._engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES ('p', :profile, :now, :now)"
        ), {"profile": profile, "now": now})
        conn.execute(text(
            "INSERT INTO conversations (id, profile, title, created_at, updated_at) "
            "VALUES (:conv, :profile, 't', :now, :now)"
        ), {"conv": conv, "profile": profile, "now": now})


def _insert(store: EventSubscriptionStorage) -> dict:
    return store.insert(
        conversation_id="c1", profile="admin", skill_name="daily-brief",
        event_type="morning", action="summarize",
    )


def test_update_fields_patches_trigger_and_action(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = _insert(store)

    updated = store.update_fields(
        row["id"], event_type="evening", action="summarize again",
        # skill_name and identity are not editable.
        skill_name="other-skill", profile="hacker", conversation_id="c99",
    )
    assert updated is not None
    assert updated["event_type"] == "evening"
    assert updated["action"] == "summarize again"
    assert updated["skill_name"] == "daily-brief"
    assert updated["profile"] == "admin"
    assert updated["id"] == row["id"]


def test_update_fields_noop_returns_row(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = _insert(store)
    same = store.update_fields(row["id"], skill_name="other")
    assert same["event_type"] == "morning"
    assert same["action"] == "summarize"


def test_update_fields_missing_id_returns_none(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    assert store.update_fields("nope", action="x") is None
