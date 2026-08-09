"""Deriving a run's message origin from the conversation row.

Web-vs-channel is decided by the conversation's channel TYPE, not by whether it
has a channel id: web/CLI conversations auto-bind to the profile's hidden
``main`` channel. Deriving from the conversation (rather than the turn's
metadata) is what makes the resulting prompt block constant for the whole
conversation, including when an operator types into a channel sender's
conversation from the web composer.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.agent.stream_runner import _resolve_message_origin  # noqa: E402


class _Store:
    def __init__(self, channel=None, sender=None):
        self._channel = channel
        self._sender = sender
        self.sender_lookups = 0

    async def get_channel(self, cid):
        if self._channel and self._channel["id"] == cid:
            return dict(self._channel)
        return None

    async def get_sender_by_conversation(self, conversation_id):
        self.sender_lookups += 1
        return dict(self._sender) if self._sender else None


def _resolve(store, conv, *, event_run=False, conversation_id="c1"):
    return asyncio.run(
        _resolve_message_origin(store, conv, conversation_id, event_run=event_run)
    )


def test_main_channel_is_web_ui():
    store = _Store(channel={"id": "main1", "channel_type": "main"})
    assert _resolve(store, {"channel_id": "main1"}) == {"source": "web_ui"}
    # No sender lookup wasted on a web conversation.
    assert store.sender_lookups == 0


def test_missing_channel_row_falls_back_to_web_ui():
    store = _Store(channel=None)
    assert _resolve(store, {"channel_id": "gone"}) == {"source": "web_ui"}


def test_conversation_without_channel_is_web_ui():
    store = _Store(channel=None)
    assert _resolve(store, {"channel_id": None}) == {"source": "web_ui"}


def test_external_channel_carries_channel_and_sender():
    store = _Store(
        channel={"id": "ch1", "channel_type": "telegram"},
        sender={"sender_id": "84986664411", "display_name": "Lee Nguyen"},
    )
    origin = _resolve(store, {"channel_id": "ch1"})

    assert origin["source"] == "channel"
    assert origin["channel_id"] == "ch1"
    assert origin["channel_type"] == "telegram"
    # Human label comes from the shipped channel catalog TOML.
    assert origin["channel_name"] == "Telegram"
    assert origin["sender_id"] == "84986664411"
    assert origin["sender_display_name"] == "Lee Nguyen"


def test_external_channel_without_sender_row():
    store = _Store(channel={"id": "ch1", "channel_type": "telegram"}, sender=None)
    origin = _resolve(store, {"channel_id": "ch1"})
    assert origin["source"] == "channel"
    assert origin["sender_id"] is None


def test_unknown_channel_type_falls_back_to_the_type_as_name():
    store = _Store(channel={"id": "ch1", "channel_type": "not-a-real-platform"})
    origin = _resolve(store, {"channel_id": "ch1"})
    assert origin["channel_name"] == "not-a-real-platform"


def test_event_runs_get_no_origin():
    """Event runs describe their trigger elsewhere and are a disjoint cache pool."""
    store = _Store(channel={"id": "ch1", "channel_type": "telegram"})
    assert _resolve(store, {"channel_id": "ch1"}, event_run=True) is None


def test_missing_conversation_yields_no_origin():
    assert _resolve(_Store(), None) is None


def test_storage_failure_is_swallowed():
    """Prompt garnish must never fail a run."""
    class _Boom:
        async def get_channel(self, cid):
            raise RuntimeError("db down")

    assert _resolve(_Boom(), {"channel_id": "ch1"}) is None


def test_sender_lookup_failure_is_swallowed():
    class _Partial(_Store):
        async def get_sender_by_conversation(self, conversation_id):
            raise RuntimeError("db down")

    store = _Partial(channel={"id": "ch1", "channel_type": "telegram"})
    assert _resolve(store, {"channel_id": "ch1"}) is None
