"""Channels API: delete a client completely (`DELETE .../senders/{sender_id}`).

The promise this endpoint makes is strong — "as if that client had never
messaged" — so the tests are mostly about leftovers. Each one pins a specific
thing that would otherwise survive and make the promise false: the sender row,
the conversation and its messages, the static notification recipient list (the
one leftover that would keep actively delivering to a deleted person), the
adapter's in-memory state, and long-term facts learned from them.

Drives the route endpoints directly with a fake Request over an in-memory
storage stand-in, mirroring test_channels_sender_history / test_channels_direct_message.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import pytest

import app.api.channels as api_channels
from app.api.channels import get_channel_routes

_SENDER = "/api/channels/{channel_id}/senders/{sender_id}"


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="p1", path_params=None, method="DELETE"):
    async def _json():
        raise ValueError("no body")
    # The sender-detail route is a method dispatcher (PATCH updates, DELETE
    # removes the client), so the fake request has to carry a method.
    return SimpleNamespace(
        method=method,
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        json=_json,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Adapter:
    """Adapter stand-in that records the forget call and holds live config."""

    def __init__(self, channel: dict) -> None:
        self.channel = channel
        self.channel_id = channel["id"]
        self.channel_type = channel["channel_type"]
        self.profile = channel["profile"]
        self.forgotten: list[str] = []

    def forget_sender(self, sender_id: str) -> None:
        self.forgotten.append(sender_id)


class _Store:
    def __init__(self, *, channel, senders=(), messages=0):
        self._channel = dict(channel)
        self._senders = [dict(s) for s in senders]
        self._messages = messages
        self.deleted_conversations: list[str] = []
        self.cleared: list[str] = []
        self.channel_updates: list[dict] = []

    async def get_channel(self, cid):
        return dict(self._channel) if cid == self._channel["id"] else None

    async def update_channel(self, channel_id, **fields):
        self.channel_updates.append(dict(fields))
        self._channel.update(fields)
        return dict(self._channel)

    async def list_senders(self, cid):
        return [dict(s) for s in self._senders]

    async def delete_sender(self, row_id):
        before = len(self._senders)
        self._senders = [s for s in self._senders if s["id"] != row_id]
        return len(self._senders) < before

    async def clear_conversation_messages(self, conversation_id):
        self.cleared.append(conversation_id)
        n, self._messages = self._messages, 0
        return n

    async def delete_conversation(self, conversation_id):
        self.deleted_conversations.append(conversation_id)
        return True

    async def get_conversation(self, conversation_id):
        return {"id": conversation_id, "profile": self._channel["profile"]}


def _channel(profile="p1", config=None, mode="bot"):
    return {"id": "ch1", "profile": profile, "channel_type": "telegram",
            "mode": mode, "config": config if config is not None else {}}


def _sender(sender_id="s1", conversation_id="c1", **kw):
    return {"id": f"row-{sender_id}", "channel_id": "ch1", "sender_id": sender_id,
            "display_name": "Lee", "phone": "84901234567", "wa_lid": None,
            "authenticated": True, "pending_otp": None,
            "pending_otp_expires_at": None,
            "conversation_id": conversation_id, **kw}


@pytest.fixture(autouse=True)
def _neutralize_side_effects(monkeypatch):
    """Stub the teardown collaborators the endpoint reaches for.

    They are exercised by their own suites; here they would need a live event
    loop, on-disk profile dirs and a real DB. Each stub records that it ran so
    the tests can assert the teardown actually happened.
    """
    calls: dict[str, list] = {
        "dependents": [], "memories": [], "uploads": [], "context": [],
    }

    async def _dependents(storage, conv_id):
        calls["dependents"].append(conv_id)

    async def _memories(profile, conv_id):
        calls["memories"].append((profile, conv_id))
        return 2

    import app.reset._conversations as convs
    import app.reset._senders as senders_mod
    monkeypatch.setattr(convs, "cleanup_conversation_dependents", _dependents)
    monkeypatch.setattr(senders_mod, "_forget_conversation_memories", _memories)
    monkeypatch.setattr(
        senders_mod, "_remove_uploads",
        lambda profile, conv_id: calls["uploads"].append(conv_id),
    )
    monkeypatch.setattr(
        senders_mod, "_clear_in_memory_context",
        lambda conv_id: calls["context"].append(conv_id),
    )

    # The endpoint publishes admin change events; they need running SSE buses.
    for mod, name in (
        ("app.api.events", "publish_skill_events_admin_changed"),
        ("app.api.file_watchers", "publish_file_watchers_admin_changed"),
        ("app.events.conversations_list_bus", "publish_conversations_changed"),
    ):
        monkeypatch.setattr(
            __import__(mod, fromlist=[name]), name, lambda *_a, **_k: None,
        )
    return calls


def _call(store, path_params, *, adapter=None, active=False, username="p1"):
    """Invoke the DELETE handler with the registry and stream bus patched."""
    reg_backup = api_channels.get_channel_registry
    api_channels.get_channel_registry = lambda *a, **k: SimpleNamespace(
        get_adapter=lambda cid: adapter,
    )
    import app.events.stream_bus as sb
    bus_backup = sb.get_event_stream_bus
    sb.get_event_stream_bus = lambda *a, **k: SimpleNamespace(
        is_active=lambda cid: active,
    )
    try:
        handler = _handler(store, _SENDER, "DELETE")
        return asyncio.run(handler(_req(username, path_params)))
    finally:
        api_channels.get_channel_registry = reg_backup
        sb.get_event_stream_bus = bus_backup


_PP = {"channel_id": "ch1", "sender_id": "s1"}


def test_deletes_sender_conversation_and_messages(_neutralize_side_effects):
    store = _Store(channel=_channel(), senders=[_sender()], messages=42)
    ch = _channel()
    adapter = _Adapter(ch)

    resp = _call(store, _PP, adapter=adapter)

    assert resp.status_code == 200
    out = _body(resp)
    assert out["success"] is True
    assert out["deleted_messages"] == 42
    assert out["conversation_id"] == "c1"
    # The person is gone, not merely emptied.
    assert store._senders == []
    assert store.deleted_conversations == ["c1"]
    # And the conversation's dependents were torn down before it went.
    assert _neutralize_side_effects["dependents"] == ["c1"]
    assert _neutralize_side_effects["uploads"] == ["c1"]
    assert _neutralize_side_effects["context"] == ["c1"]


def test_forgets_the_adapters_in_memory_state():
    """Leaving live state behind makes the "never messaged" claim false."""
    store = _Store(channel=_channel(), senders=[_sender()])
    adapter = _Adapter(_channel())
    _call(store, _PP, adapter=adapter)
    assert adapter.forgotten == ["s1"]


def test_works_with_no_running_adapter():
    store = _Store(channel=_channel(), senders=[_sender()])
    resp = _call(store, _PP, adapter=None)
    assert resp.status_code == 200
    assert store._senders == []


def test_sender_without_a_conversation_is_still_deleted():
    """A pending or revoked contact has a row but no conversation."""
    store = _Store(channel=_channel(), senders=[_sender(conversation_id=None)])
    resp = _call(store, _PP)
    out = _body(resp)
    assert out["success"] is True
    assert out["conversation_id"] is None
    assert out["deleted_messages"] == 0
    assert store._senders == []
    assert store.deleted_conversations == []


def test_forgets_memories_learned_from_that_conversation(_neutralize_side_effects):
    store = _Store(channel=_channel(), senders=[_sender()])
    out = _body(_call(store, _PP))
    assert out["forgot_memories"] == 2
    assert _neutralize_side_effects["memories"] == [("p1", "c1")]


def test_prunes_the_static_notification_recipient_list():
    """Otherwise a deleted client keeps receiving notification pushes."""
    ch = _channel(config={"target_chat_ids": "s1,s2"}, mode="notification")
    store = _Store(channel=ch, senders=[_sender()])
    adapter = _Adapter(dict(ch))

    out = _body(_call(store, _PP, adapter=adapter))

    assert out["unsubscribed_target"] is True
    assert store.channel_updates[0]["config"]["target_chat_ids"] == "s2"
    # The running adapter reads its own in-memory copy, so that must change too.
    assert adapter.channel["config"]["target_chat_ids"] == "s2"


def test_prunes_target_chat_ids_given_as_a_list():
    ch = _channel(config={"target_chat_ids": ["s1", "s2"]}, mode="notification")
    store = _Store(channel=ch, senders=[_sender()])
    out = _body(_call(store, _PP))
    assert out["unsubscribed_target"] is True
    assert store.channel_updates[0]["config"]["target_chat_ids"] == ["s2"]


def test_untouched_target_list_reports_no_change():
    ch = _channel(config={"target_chat_ids": "someone-else"}, mode="notification")
    store = _Store(channel=ch, senders=[_sender()])
    out = _body(_call(store, _PP))
    assert out["unsubscribed_target"] is False
    assert store.channel_updates == []


def test_refuses_while_a_run_is_in_progress():
    store = _Store(channel=_channel(), senders=[_sender()])
    resp = _call(store, _PP, active=True)
    assert resp.status_code == 409
    # Nothing may be touched — the row must survive for a retry.
    assert len(store._senders) == 1
    assert store.deleted_conversations == []


def test_404s_unknown_sender():
    store = _Store(channel=_channel(), senders=[])
    resp = _call(store, _PP)
    assert resp.status_code == 404


def test_404s_unknown_channel():
    store = _Store(channel=_channel(), senders=[_sender()])
    resp = _call(store, {"channel_id": "nope", "sender_id": "s1"})
    assert resp.status_code == 404


def test_403s_other_profiles_channel():
    store = _Store(channel=_channel(profile="other"), senders=[_sender()])
    resp = _call(store, _PP)
    assert resp.status_code == 403


def test_401s_unauthenticated():
    store = _Store(channel=_channel(), senders=[_sender()])
    handler = _handler(store, _SENDER, "DELETE")
    req = SimpleNamespace(
        method="DELETE",
        user=SimpleNamespace(is_authenticated=False, username=""),
        path_params=_PP, json=lambda: None,
    )
    assert asyncio.run(handler(req)).status_code == 401


def test_patch_and_delete_share_one_dispatcher():
    """The new DELETE shares its path with PATCH — both must stay routable."""
    store = _Store(channel=_channel(), senders=[_sender()])
    route = next(
        r for r in get_channel_routes(store) if r.path == _SENDER
    )
    assert {"PATCH", "DELETE"} <= route.methods
    assert route.endpoint.__name__ == "handle_sender_detail_dispatch"


def test_unsupported_method_is_405():
    store = _Store(channel=_channel(), senders=[_sender()])
    handler = _handler(store, _SENDER, "DELETE")
    resp = asyncio.run(handler(_req(path_params=_PP, method="PUT")))
    assert resp.status_code == 405
