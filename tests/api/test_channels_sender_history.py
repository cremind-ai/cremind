"""Channels page: per-sender usage totals + clear-history endpoint.

Drives the channel route endpoints directly with a fake Request over an
in-memory storage stand-in (no DB), mirroring test_channels_subscribe_auth.

``DELETE /api/channels/{id}/senders/{sender_id}/messages`` wipes one
subscriber's messages while KEEPING their conversation, so the sender's next
message continues in it and their usage totals survive. ``GET .../senders``
carries those totals so the admin page needn't open each conversation.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api.channels import get_channel_routes

_SENDERS = "/api/channels/{channel_id}/senders"
_CLEAR = "/api/channels/{channel_id}/senders/{sender_id}/messages"


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="p1", path_params=None):
    async def _json():
        raise ValueError("no body")
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        json=_json,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Store:
    def __init__(self, *, channel, senders=()):
        self._channel = channel
        self._senders = [dict(s) for s in senders]
        self.cleared: list[str] = []

    async def get_channel(self, cid):
        return dict(self._channel) if cid == self._channel["id"] else None

    async def list_senders(self, cid):
        return [dict(s) for s in self._senders]

    async def update_sender(self, row_id, **fields):
        for s in self._senders:
            if s["id"] == row_id:
                s.update(fields)
                return dict(s)
        return None

    async def clear_conversation_messages(self, conversation_id):
        self.cleared.append(conversation_id)
        return 7


def _channel(profile="p1"):
    return {"id": "ch1", "profile": profile, "channel_type": "telegram",
            "mode": "bot", "config": {}}


def _sender(sender_id="s1", conversation_id="c1", **kw):
    return {"id": f"row-{sender_id}", "channel_id": "ch1",
            "sender_id": sender_id, "display_name": "Lee",
            "authenticated": True, "pending_otp": None,
            "pending_otp_expires_at": None,
            "conversation_id": conversation_id, **kw}


class _Bus:
    def __init__(self, active=False):
        self._active = active
        self.discarded: list[str] = []

    def is_active(self, cid):
        return self._active

    async def discard(self, cid):
        self.discarded.append(cid)


@pytest.fixture
def quiet_side_effects(monkeypatch):
    """Stub the bus/queue/plan teardown so tests stay pure in-memory."""
    bus = _Bus()
    monkeypatch.setattr(
        "app.events.stream_bus.get_event_stream_bus", lambda: bus,
    )
    monkeypatch.setattr("app.events.queue.discard_queue", lambda cid: None)
    monkeypatch.setattr(
        "app.utils.plans_dir.remove_conversation_plans", lambda p, c: None,
    )
    monkeypatch.setattr(
        "app.events.conversations_list_bus.publish_conversations_changed",
        lambda profile: None,
    )
    return bus


# ── senders list carries usage ─────────────────────────────────────────────


def test_senders_list_includes_usage_totals(monkeypatch):
    store = _Store(channel=_channel(), senders=[
        _sender("s1", "c1"), _sender("s2", "c2"), _sender("s3", None),
    ])
    rollups = {"c1": {"total_tokens": 120, "total_usd": 0.25, "input_tokens": 100,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 20,
                      "request_count": 3}}

    async def rollup_by_conversation(ids):
        assert set(ids) == {"c1", "c2"}  # the sender with no conversation is skipped
        return rollups

    monkeypatch.setattr(
        "app.storage.get_usage_storage",
        lambda: SimpleNamespace(rollup_by_conversation=rollup_by_conversation),
    )

    get = _handler(store, _SENDERS, "GET")
    resp = asyncio.run(get(_req(path_params={"channel_id": "ch1"})))
    senders = _body(resp)["senders"]

    by_id = {s["sender_id"]: s for s in senders}
    assert by_id["s1"]["usage"]["total_tokens"] == 120
    # No usage rows yet, and no conversation at all, both render as null.
    assert by_id["s2"]["usage"] is None
    assert by_id["s3"]["usage"] is None


def test_senders_list_survives_usage_failure(monkeypatch):
    """A usage hiccup must not break subscriber management."""
    store = _Store(channel=_channel(), senders=[_sender("s1", "c1")])

    async def boom(ids):
        raise RuntimeError("usage table on fire")

    monkeypatch.setattr(
        "app.storage.get_usage_storage",
        lambda: SimpleNamespace(rollup_by_conversation=boom),
    )
    get = _handler(store, _SENDERS, "GET")
    resp = asyncio.run(get(_req(path_params={"channel_id": "ch1"})))
    assert resp.status_code == 200
    assert _body(resp)["senders"][0]["usage"] is None


# ── clear history ──────────────────────────────────────────────────────────


def test_clear_history_wipes_messages_and_keeps_conversation(quiet_side_effects):
    store = _Store(channel=_channel(), senders=[_sender("s1", "c1")])
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(
        _req(path_params={"channel_id": "ch1", "sender_id": "s1"})
    ))
    assert resp.status_code == 200
    assert _body(resp) == {
        "success": True, "conversation_id": "c1", "cleared_messages": 7,
    }
    assert store.cleared == ["c1"]
    # The link is untouched: the sender keeps the same conversation.
    assert store._senders[0]["conversation_id"] == "c1"
    # Replay buffer dropped so open viewers don't replay wiped turns.
    assert quiet_side_effects.discarded == ["c1"]


def test_clear_history_no_conversation_is_a_noop(quiet_side_effects):
    store = _Store(channel=_channel(), senders=[_sender("s1", None)])
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(
        _req(path_params={"channel_id": "ch1", "sender_id": "s1"})
    ))
    assert resp.status_code == 200
    assert _body(resp)["cleared_messages"] == 0
    assert store.cleared == []


def test_clear_history_conflicts_while_running(monkeypatch):
    store = _Store(channel=_channel(), senders=[_sender("s1", "c1")])
    monkeypatch.setattr(
        "app.events.stream_bus.get_event_stream_bus", lambda: _Bus(active=True),
    )
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(
        _req(path_params={"channel_id": "ch1", "sender_id": "s1"})
    ))
    assert resp.status_code == 409
    assert store.cleared == []


def test_clear_history_unknown_sender_is_404(quiet_side_effects):
    store = _Store(channel=_channel(), senders=[_sender("s1", "c1")])
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(
        _req(path_params={"channel_id": "ch1", "sender_id": "nope"})
    ))
    assert resp.status_code == 404
    assert store.cleared == []


def test_clear_history_unknown_channel_is_404(quiet_side_effects):
    store = _Store(channel=_channel(), senders=[_sender("s1", "c1")])
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(
        _req(path_params={"channel_id": "other", "sender_id": "s1"})
    ))
    assert resp.status_code == 404


def test_clear_history_other_profile_is_403(quiet_side_effects):
    store = _Store(channel=_channel(profile="someone-else"),
                   senders=[_sender("s1", "c1")])
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(
        _req(username="p1", path_params={"channel_id": "ch1", "sender_id": "s1"})
    ))
    assert resp.status_code == 403
    assert store.cleared == []


def test_clear_history_requires_auth(quiet_side_effects):
    store = _Store(channel=_channel(), senders=[_sender("s1", "c1")])
    delete = _handler(store, _CLEAR, "DELETE")

    resp = asyncio.run(delete(SimpleNamespace(
        user=SimpleNamespace(is_authenticated=False),
        path_params={"channel_id": "ch1", "sender_id": "s1"},
    )))
    assert resp.status_code == 401
