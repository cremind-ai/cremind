"""Skill-event subscription storage: insert, editable-field patching, repoint.

Mirrors ``tests/storage/test_schedule_event_storage.py``. Locks in that
``update_fields`` edits only ``event_type`` / ``action`` and leaves
``skill_name`` and the identity columns alone — and that ``repoint_skill``, the
one caller allowed past that guard, moves exactly the rows it should.
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


def _seed(store: EventSubscriptionStorage, *, profile="admin", conv="c1", pid="p") -> None:
    from sqlalchemy import text

    now = time.time()
    with store._engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO profiles (id, name, created_at, updated_at) "
            "VALUES (:pid, :profile, :now, :now)"
        ), {"pid": pid, "profile": profile, "now": now})
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


# --- repoint_skill: rescuing subscriptions whose skill stopped producing the event ---


def _insert_gmail_rows(store: EventSubscriptionStorage) -> tuple[dict, dict]:
    """One canonical-id row and one bare-name row, both on ``new_email``."""
    canonical = store.insert(
        conversation_id="c1", profile="admin", skill_name="admin__gmail",
        event_type="new_email", action="file the invoice",
    )
    bare = store.insert(
        conversation_id="c1", profile="admin", skill_name="gmail",
        event_type="new_email", action="ping me",
    )
    return canonical, bare


def test_repoint_skill_rewrites_canonical_and_bare_forms(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    canonical, bare = _insert_gmail_rows(store)

    ids = store.repoint_skill(
        profile="admin",
        old_skill_names=["admin__gmail", "gmail"],
        new_skill_name="admin__imap_email",
        event_type="new_email",
    )

    assert set(ids) == {canonical["id"], bare["id"]}
    for row_id in ids:
        row = store.get(row_id)
        assert row["skill_name"] == "admin__imap_email"
        assert row["paused"] is True


def test_repoint_skill_leaves_other_events_skills_and_profiles_alone(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    _seed(store, profile="bob", conv="c2", pid="p2")
    _insert_gmail_rows(store)
    other_event = store.insert(
        conversation_id="c1", profile="admin", skill_name="admin__gmail",
        event_type="label_added", action="x",
    )
    other_skill = _insert(store)
    other_profile = store.insert(
        conversation_id="c2", profile="bob", skill_name="bob__gmail",
        event_type="new_email", action="x",
    )

    store.repoint_skill(
        profile="admin",
        old_skill_names=["admin__gmail", "gmail"],
        new_skill_name="admin__imap_email",
        event_type="new_email",
    )

    assert store.get(other_event["id"])["skill_name"] == "admin__gmail"
    assert store.get(other_skill["id"])["skill_name"] == "daily-brief"
    assert store.get(other_skill["id"])["paused"] is False
    assert store.get(other_profile["id"])["skill_name"] == "bob__gmail"


def test_repoint_skill_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    _insert_gmail_rows(store)
    kwargs = dict(
        profile="admin",
        old_skill_names=["admin__gmail", "gmail"],
        new_skill_name="admin__imap_email",
        event_type="new_email",
    )

    assert len(store.repoint_skill(**kwargs)) == 2
    # The rewrite empties the set the query matches — this is what makes the
    # boot-time caller fire its notification exactly once.
    assert store.repoint_skill(**kwargs) == []


def test_repoint_skill_preserves_action_and_created_at(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    canonical, _ = _insert_gmail_rows(store)

    store.repoint_skill(
        profile="admin",
        old_skill_names=["admin__gmail", "gmail"],
        new_skill_name="admin__imap_email",
        event_type="new_email",
    )

    row = store.get(canonical["id"])
    assert row["action"] == "file the invoice"
    assert row["created_at"] == canonical["created_at"]
    assert row["conversation_id"] == "c1"


def test_repoint_skill_without_event_type_moves_every_event(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    _insert_gmail_rows(store)
    other_event = store.insert(
        conversation_id="c1", profile="admin", skill_name="admin__gmail",
        event_type="label_added", action="x",
    )

    ids = store.repoint_skill(
        profile="admin",
        old_skill_names=["admin__gmail", "gmail"],
        new_skill_name="admin__imap_email",
    )

    assert other_event["id"] in ids


def test_repoint_skill_with_no_old_names_is_a_noop(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    _insert_gmail_rows(store)
    assert store.repoint_skill(
        profile="admin", old_skill_names=[], new_skill_name="admin__imap_email",
    ) == []
