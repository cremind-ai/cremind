"""Boot repair for subscriptions whose skill stopped producing their event.

Gmail lost its push listener, so every ``new_email`` subscription pointing at it
is orphaned: nothing can fire it, yet the Events page still lists it as live.
These tests pin the repair — rows move to imap-email paused, the user is told
once, and a boot with nothing to fix stays silent.
"""

from __future__ import annotations

import pytest

import app.skills.sync as sync


class _FakeStore:
    def __init__(self, ids: list[str]):
        self._ids = ids
        self.calls: list[dict] = []

    def repoint_skill(self, **kwargs):
        self.calls.append(kwargs)
        # Second call finds nothing: the first one emptied the matching set.
        ids, self._ids = self._ids, []
        return ids


@pytest.fixture
def wiring(monkeypatch):
    """Capture notifications and admin-bus ticks instead of emitting them."""
    pushed: list[dict] = []
    ticks: list[str] = []

    class _Buffer:
        def push(self, **kwargs):
            pushed.append(kwargs)

    class _Bus:
        def publish(self, profile, entry):
            ticks.append(profile)

    import app.events.notifications_buffer as nb
    import app.events.skill_events_admin_bus as ab

    monkeypatch.setattr(nb, "get_event_notifications", lambda: _Buffer())
    monkeypatch.setattr(ab, "get_skill_events_admin_stream_bus", lambda: _Bus())
    return pushed, ticks


def _install_store(monkeypatch, store) -> None:
    import app.storage as storage

    monkeypatch.setattr(storage, "get_event_subscription_storage", lambda *a, **k: store)


def test_gmail_rows_are_repointed_to_imap_email_and_paused(monkeypatch, wiring):
    pushed, ticks = wiring
    store = _FakeStore(["s1", "s2"])
    _install_store(monkeypatch, store)

    sync._repoint_orphaned_event_subscriptions("admin")

    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["profile"] == "admin"
    assert call["new_skill_name"] == "admin__imap_email"
    assert call["event_type"] == "new_email"
    assert call["pause"] is True
    # Both the canonical tool id and the bare directory name must be caught.
    assert "admin__gmail" in call["old_skill_names"]
    assert "gmail" in call["old_skill_names"]
    assert len(pushed) == 1
    assert ticks == ["admin"]


def test_the_notification_deep_links_to_the_successor_skill(monkeypatch, wiring):
    pushed, _ = wiring
    _install_store(monkeypatch, _FakeStore(["s1", "s2"]))

    sync._repoint_orphaned_event_subscriptions("admin")

    entry = pushed[0]
    # This kind is what makes the notification click through to the skill's
    # settings pane — where the missing IMAP credentials are entered.
    assert entry["kind"] == "skill_register_required"
    assert entry["priority"] == "high"
    assert entry["extra"]["skill_id"] == "imap-email"
    assert "2 email automation" in entry["message_preview"]
    assert "paused" in entry["message_preview"]


def test_nothing_to_repoint_notifies_nobody(monkeypatch, wiring):
    pushed, ticks = wiring
    _install_store(monkeypatch, _FakeStore([]))

    sync._repoint_orphaned_event_subscriptions("admin")

    assert pushed == []
    assert ticks == []


def test_a_second_boot_does_not_re_notify(monkeypatch, wiring):
    pushed, _ = wiring
    store = _FakeStore(["s1"])
    _install_store(monkeypatch, store)

    sync._repoint_orphaned_event_subscriptions("admin")
    sync._repoint_orphaned_event_subscriptions("admin")

    assert len(pushed) == 1


def test_a_storage_failure_never_blocks_boot(monkeypatch, wiring):
    pushed, _ = wiring

    class _Broken:
        def repoint_skill(self, **kwargs):
            raise RuntimeError("db is down")

    _install_store(monkeypatch, _Broken())

    sync._repoint_orphaned_event_subscriptions("admin")  # must not raise

    assert pushed == []
