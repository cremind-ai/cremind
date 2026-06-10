"""Tests for autostart-registration duplicate detection.

Process identity for an autostart registration is the triple
``(profile, command, working_dir)`` — *not* the command string alone. Many
built-in skills declare the identical listener command
(``uv run scripts/event_listener.py``) yet run from different skill
directories; they are distinct applications and must not be flagged as
duplicates of one another. These tests lock that in.

The harness mirrors ``tests/storage/test_backup.py``: a real on-disk SQLite
provider with only the ``autostart_processes`` table created from the ORM's
own metadata, so the columns match production exactly.
"""

from __future__ import annotations

from pathlib import Path

from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base.metadata
from app.databases.sqlite import SqliteDatabaseProvider
from app.storage.autostart_storage import AutostartStorage


_LISTENER_CMD = "uv run scripts/event_listener.py"


def _make_store(tmp_path: Path) -> AutostartStorage:
    provider = SqliteDatabaseProvider(str(tmp_path / "autostart.db"))
    Base.metadata.tables["autostart_processes"].create(bind=provider.sync_engine())
    return AutostartStorage(provider)


def test_same_command_different_skill_dir_is_not_duplicate(tmp_path: Path) -> None:
    """The bug fix: identical command, different skill dir → NOT a duplicate."""
    store = _make_store(tmp_path)
    store.insert(
        profile="p",
        command=_LISTENER_CMD,
        working_dir="/skills/gcalendar",
        is_pty=False,
    )

    # A *different* skill (gmail) sharing the same command string must be free
    # to register — it is a different application.
    assert (
        store.find_duplicate("p", _LISTENER_CMD, working_dir="/skills/gmail")
        is None
    )


def test_identical_triple_is_duplicate(tmp_path: Path) -> None:
    """True duplicate still caught: same (profile, command, working_dir)."""
    store = _make_store(tmp_path)
    row = store.insert(
        profile="p",
        command=_LISTENER_CMD,
        working_dir="/skills/gcalendar",
        is_pty=False,
    )

    found = store.find_duplicate("p", _LISTENER_CMD, working_dir="/skills/gcalendar")
    assert found is not None
    assert found["id"] == row["id"]


def test_different_profile_is_not_duplicate(tmp_path: Path) -> None:
    """Registrations are profile-scoped: another profile is never a duplicate."""
    store = _make_store(tmp_path)
    store.insert(
        profile="alice",
        command=_LISTENER_CMD,
        working_dir="/skills/gcalendar",
        is_pty=False,
    )

    assert (
        store.find_duplicate("bob", _LISTENER_CMD, working_dir="/skills/gcalendar")
        is None
    )
