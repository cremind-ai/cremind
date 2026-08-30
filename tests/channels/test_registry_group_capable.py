"""Which platforms are offered as somewhere a group room can live.

The list is computed from the adapter classes rather than written down, because
a written one drifts the moment an adapter learns rooms: the settings page would
go on offering a platform whose adapter silently ingests nothing, or hide one
that works. Telegram carries rooms, on both its transports; Messenger's Page bot
has no room to join, so it must not be offered.

Reading it must also stay SDK-free — the platform SDKs are optional extras and
are not installed here — which these tests exercise by walking the real
catalogue through the real resolver.
"""

from __future__ import annotations

import pytest

import app.channels.registry as registry_module
from app.channels.registry import group_capable_channel_types


@pytest.fixture(autouse=True)
def _clean_cache():
    """The answer is memoised per process; no test may inherit another's."""
    registry_module._group_capable_cache = None
    yield
    registry_module._group_capable_cache = None


def test_telegram_is_offered_and_messenger_is_not():
    types = {row["channel_type"] for row in group_capable_channel_types()}

    assert "telegram" in types
    assert "messenger" not in types


def test_an_entry_carries_what_the_settings_page_renders():
    telegram = next(
        row for row in group_capable_channel_types()
        if row["channel_type"] == "telegram"
    )

    assert telegram == {
        "channel_type": "telegram",
        "display_name": "Telegram",
        "icon": "mdi:telegram",
    }


def test_an_adapter_that_learns_rooms_needs_listing_nowhere_else(monkeypatch):
    """The point of computing this: a new room-capable adapter is offered by
    declaring ``supports_group_chats``, with no second place to remember."""
    real = registry_module._resolve_adapter_class

    class _RoomCapable:
        supports_group_chats = True

    def _fake(channel_type, mode):
        return _RoomCapable if channel_type == "messenger" else real(channel_type, mode)

    monkeypatch.setattr(registry_module, "_resolve_adapter_class", _fake)

    assert "messenger" in {
        row["channel_type"] for row in group_capable_channel_types()
    }


def test_an_adapter_that_cannot_be_imported_costs_only_its_own_type(monkeypatch):
    """A platform whose module will not import (a missing optional SDK, an
    adapter that does not exist yet) must not take the rest of the list with
    it — the settings page still has to offer the platforms that do work."""
    real = registry_module._resolve_adapter_class

    def _fake(channel_type, mode):
        if channel_type == "telegram":
            raise ImportError("python-telegram-bot is not installed")
        return real(channel_type, mode)

    monkeypatch.setattr(registry_module, "_resolve_adapter_class", _fake)

    assert "telegram" not in {
        row["channel_type"] for row in group_capable_channel_types()
    }


def test_the_catalogue_is_walked_once_and_the_answer_is_not_shared(monkeypatch):
    """Read on every settings-page load and every binding attempt, off code that
    cannot change while the process runs."""
    import app.config as config_module

    walks: list[int] = []
    real = config_module.load_all_channel_catalogs

    def _counted():
        walks.append(1)
        return real()

    monkeypatch.setattr(config_module, "load_all_channel_catalogs", _counted)

    first = group_capable_channel_types()
    first.append({"channel_type": "made-up", "display_name": "", "icon": ""})
    second = group_capable_channel_types()

    assert len(walks) == 1
    assert "made-up" not in {row["channel_type"] for row in second}
