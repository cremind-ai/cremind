"""Channels API: direct messages to individual clients + the sender phone PATCH.

Drives the route endpoints directly with a fake Request over an in-memory
storage stand-in (no DB), mirroring test_channels_sender_history.

Two contracts are pinned here. ``POST /api/channels/{id}/message`` previews
unless explicitly told to send, and reports per-recipient failures *inside* a
200 — the request succeeded even when a recipient didn't, so a partly-bad
contact list is data, not an HTTP error. ``PATCH .../senders/{sender_id}``
gained ``phone`` while keeping the existing approve/revoke behaviour byte-for-
byte, since the Subscribers UI and ``channels approve`` depend on it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import app.api.channels as api_channels
from app.api.channels import get_channel_routes

_MESSAGE = "/api/channels/{channel_id}/message"
_PATCH_SENDER = "/api/channels/{channel_id}/senders/{sender_id}"


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(body=None, username="p1", path_params=None, method="POST"):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body
    # ``method`` matters because the sender-detail route is a method dispatcher
    # (PATCH updates, DELETE removes the client), same as the real request.
    return SimpleNamespace(
        method=method,
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        json=_json,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Adapter:
    def __init__(self, channel_type="whatsapp", channel_id="ch1", profile="p1"):
        self.channel_type = channel_type
        self.channel_id = channel_id
        self.profile = profile
        self.sent: list[tuple[str, str]] = []
        self._locks: dict = {}

    def _inbound_lock(self, sender_id):
        return self._locks.setdefault(sender_id, asyncio.Lock())

    async def send_strict(self, sender_id, text):
        self.sent.append((sender_id, text))
        return 1

    async def resolve_phone(self, phone):
        return {"exists": True, "jid": f"{phone}@s.whatsapp.net", "lid": None}


class _Store:
    def __init__(self, *, channel, senders=()):
        self._channel = channel
        self._senders = [dict(s) for s in senders]
        self.messages: list[dict] = []

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

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None,
                                   phone=None, wa_lid=None):
        row = next((s for s in self._senders if s["sender_id"] == sender_id), None)
        if row is None:
            row = {"id": f"row-{sender_id}", "channel_id": channel_id,
                   "sender_id": sender_id, "display_name": display_name,
                   "phone": phone, "wa_lid": wa_lid, "authenticated": False,
                   "conversation_id": None}
            self._senders.append(row)
        return dict(row)

    async def ensure_sender_conversation(self, sender, profile, channel_id,
                                         display_name=None):
        return sender.get("conversation_id") or f"conv-{sender['sender_id']}"

    async def add_message(self, conversation_id, role, content=None, metadata=None, **kw):
        msg = {"id": f"m{len(self.messages)}", "conversation_id": conversation_id,
               "role": role, "content": content, "metadata": metadata}
        self.messages.append(msg)
        return msg


def _channel(profile="p1", channel_type="whatsapp"):
    return {"id": "ch1", "profile": profile, "channel_type": channel_type,
            "mode": "bot", "config": {}}


def _sender(sender_id="84901234567@s.whatsapp.net", **kw):
    return {"id": f"row-{sender_id}", "channel_id": "ch1", "sender_id": sender_id,
            "display_name": "Lee", "phone": kw.pop("phone", None), "wa_lid": None,
            "authenticated": True, "pending_otp": None,
            "pending_otp_expires_at": None,
            "conversation_id": kw.pop("conversation_id", "c1"), **kw}


def _call(store, path, method, body=None, path_params=None, adapter=None,
          username="p1"):
    """Invoke a route with the channel registry patched to yield ``adapter``.

    ``app.api.channels`` binds ``get_channel_registry`` at import time, so the
    patch has to land on that module's own name, not on app.channels.registry.
    """
    backup = api_channels.get_channel_registry
    api_channels.get_channel_registry = lambda *a, **k: SimpleNamespace(
        get_adapter=lambda cid: adapter,
    )
    try:
        handler = _handler(store, path, method)
        return asyncio.run(handler(_req(body, username, path_params, method)))
    finally:
        api_channels.get_channel_registry = backup


_PP = {"channel_id": "ch1"}


# ── POST /message ──────────────────────────────────────────────────────────


def test_message_defaults_to_preview():
    store = _Store(channel=_channel(), senders=[_sender()])
    adapter = _Adapter()
    resp = _call(store, _MESSAGE, "POST",
                 {"recipients": ["+84901234567"], "message": "hi"},
                 _PP, adapter)
    assert resp.status_code == 200
    out = _body(resp)
    assert out["dry_run"] is True
    assert out["results"][0]["status"] == "would_send"
    assert adapter.sent == [] and store.messages == []


def test_message_delivers_and_records_when_dry_run_false():
    jid = "84901234567@s.whatsapp.net"
    store = _Store(channel=_channel(), senders=[_sender(jid)])
    adapter = _Adapter()
    resp = _call(store, _MESSAGE, "POST",
                 {"recipients": [{"to": jid}], "message": "Thanks!",
                  "dry_run": False},
                 _PP, adapter)
    out = _body(resp)
    assert out["sent"] == 1
    assert adapter.sent == [(jid, "Thanks!")]
    assert store.messages[0]["metadata"]["initiated_by"] == "api"


def test_partial_failure_is_a_200_with_per_recipient_status():
    """A bad row in the list is data, not an HTTP error."""
    jid = "84901234567@s.whatsapp.net"
    store = _Store(channel=_channel(), senders=[_sender(jid)])
    adapter = _Adapter()
    resp = _call(store, _MESSAGE, "POST",
                 {"recipients": [{"to": jid}, {"to": "0901234567"}],
                  "message": "hi", "dry_run": False},
                 _PP, adapter)
    assert resp.status_code == 200
    out = _body(resp)
    assert out["sent"] == 1 and out["failed"] == 1
    assert out["results"][1]["error"] == "ambiguous_phone"


def test_message_requires_recipients():
    store = _Store(channel=_channel(), senders=[])
    resp = _call(store, _MESSAGE, "POST", {"message": "hi"}, _PP, _Adapter())
    assert resp.status_code == 400


def test_message_404s_unknown_channel():
    store = _Store(channel=_channel(), senders=[])
    resp = _call(store, _MESSAGE, "POST", {"recipients": ["U1"]},
                 {"channel_id": "nope"}, _Adapter())
    assert resp.status_code == 404


def test_message_403s_other_profiles_channel():
    store = _Store(channel=_channel(profile="other"), senders=[])
    resp = _call(store, _MESSAGE, "POST", {"recipients": ["U1"]}, _PP, _Adapter())
    assert resp.status_code == 403


def test_message_409s_when_adapter_not_running():
    store = _Store(channel=_channel(), senders=[])
    resp = _call(store, _MESSAGE, "POST", {"recipients": ["U1"]}, _PP, adapter=None)
    assert resp.status_code == 409


def test_message_401s_unauthenticated():
    store = _Store(channel=_channel(), senders=[])
    handler = _handler(store, _MESSAGE, "POST")
    req = SimpleNamespace(
        user=SimpleNamespace(is_authenticated=False, username=""),
        path_params=_PP,
        json=lambda: None,
    )
    resp = asyncio.run(handler(req))
    assert resp.status_code == 401


# ── PATCH senders (phone) ──────────────────────────────────────────────────


_SPP = {"channel_id": "ch1", "sender_id": "s1"}


def test_patch_sets_normalized_phone():
    store = _Store(channel=_channel(), senders=[_sender("s1")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"phone": "+84 90 123 4567"}, _SPP)
    assert resp.status_code == 200
    assert _body(resp)["sender"]["phone"] == "84901234567"


def test_patch_clears_phone_with_null():
    store = _Store(channel=_channel(), senders=[_sender("s1", phone="84901234567")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"phone": None}, _SPP)
    assert _body(resp)["sender"]["phone"] is None


def test_patch_rejects_unparseable_phone():
    store = _Store(channel=_channel(), senders=[_sender("s1")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"phone": "0901234567"}, _SPP)
    # National form is ambiguous without a country — refuse rather than guess.
    assert resp.status_code == 400


def test_patch_authenticated_still_works_and_clears_otp():
    store = _Store(channel=_channel(),
                   senders=[_sender("s1", authenticated=False, pending_otp="123456")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"authenticated": True}, _SPP)
    sender = _body(resp)["sender"]
    assert sender["authenticated"] is True
    assert sender["pending_otp"] is None


def test_patch_accepts_both_fields_at_once():
    store = _Store(channel=_channel(), senders=[_sender("s1", authenticated=False)])
    resp = _call(store, _PATCH_SENDER, "PATCH",
                 {"authenticated": True, "phone": "+84901234567"}, _SPP)
    sender = _body(resp)["sender"]
    assert sender["authenticated"] is True and sender["phone"] == "84901234567"


def test_patch_sets_the_send_confirmation_override():
    store = _Store(channel=_channel(), senders=[_sender("s1")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"send_confirmation": "skip"}, _SPP)
    assert resp.status_code == 200
    assert _body(resp)["sender"]["send_confirmation"] == "skip"


def test_patch_clears_the_override_with_null():
    store = _Store(channel=_channel(),
                   senders=[_sender("s1", send_confirmation="skip")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"send_confirmation": None}, _SPP)
    assert _body(resp)["sender"]["send_confirmation"] is None


def test_patch_rejects_an_unknown_confirmation_mode():
    store = _Store(channel=_channel(), senders=[_sender("s1")])
    resp = _call(store, _PATCH_SENDER, "PATCH",
                 {"send_confirmation": "sometimes"}, _SPP)
    assert resp.status_code == 400


def test_patch_with_no_recognised_field_is_400():
    store = _Store(channel=_channel(), senders=[_sender("s1")])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"nonsense": 1}, _SPP)
    assert resp.status_code == 400


def test_patch_404s_unknown_sender():
    store = _Store(channel=_channel(), senders=[])
    resp = _call(store, _PATCH_SENDER, "PATCH", {"phone": "+84901234567"}, _SPP)
    assert resp.status_code == 404
