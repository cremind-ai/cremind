"""File-watcher storage: insert + editable-field patching.

Mirrors ``tests/storage/test_schedule_event_storage.py`` — an on-disk SQLite
provider with only the relevant tables created from the ORM metadata. Locks in
that ``update_fields`` touches only the whitelisted columns and normalizes
``recursive``/``extensions`` the way ``insert`` does.
"""

from __future__ import annotations

import time
from pathlib import Path

from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.file_watcher_storage import FileWatcherSubscriptionStorage

_TABLES = ("profiles", "channels", "conversations", "file_watcher_subscriptions")


def _make_store(tmp_path: Path) -> FileWatcherSubscriptionStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "fw.db"))
    engine = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=engine, checkfirst=True)
    return FileWatcherSubscriptionStorage(provider)


def _seed(store: FileWatcherSubscriptionStorage, *, profile="admin", conv="c1") -> None:
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


def _insert(store: FileWatcherSubscriptionStorage) -> dict:
    return store.insert(
        conversation_id="c1", profile="admin", name="w", root_path="/tmp/watch",
        recursive=True, target_kind="any", event_types="created,modified",
        extensions=".py", action="do a thing",
    )


def test_update_fields_patches_only_editable(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = _insert(store)

    updated = store.update_fields(
        row["id"], root_path="/tmp/other", target_kind="file",
        event_types="modified", action="do another thing",
        # non-editable keys must be ignored
        profile="hacker", conversation_id="c99",
    )
    assert updated is not None
    assert updated["root_path"] == "/tmp/other"
    assert updated["target_kind"] == "file"
    assert updated["event_types"] == "modified"
    assert updated["action"] == "do another thing"
    # identity columns untouched
    assert updated["profile"] == "admin"
    assert updated["id"] == row["id"]
    assert updated["conversation_id"] == "c1"


def test_update_fields_normalizes_recursive_and_extensions(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = _insert(store)

    # recursive coerced to bool; empty extensions stored as NULL → "" on read.
    updated = store.update_fields(row["id"], recursive=0, extensions="")
    assert updated["recursive"] is False
    assert updated["extensions"] == ""

    updated = store.update_fields(row["id"], recursive=1, extensions=".md,.txt")
    assert updated["recursive"] is True
    assert updated["extensions"] == ".md,.txt"


def test_update_fields_noop_returns_row(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    row = _insert(store)
    # No editable keys → returns the current row unchanged.
    same = store.update_fields(row["id"], profile="hacker")
    assert same["action"] == "do a thing"


def test_update_fields_missing_id_returns_none(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed(store)
    assert store.update_fields("nope", action="x") is None
