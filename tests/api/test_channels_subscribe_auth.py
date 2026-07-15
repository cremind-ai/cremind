"""Notification subscription-auth API: subscribe_auth validation + approve/revoke.

Drives the channel route endpoints directly with a fake Request backed by a
lightweight in-memory storage stand-in (no DB), plus unit coverage of the
``_validate_subscribe_auth`` guard.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

from app.api.channels import (
    _validate_subscribe_auth,
    create_channel_for_profile,
    get_channel_routes,
)


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _req(username="p1", path_params=None, body=None):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=path_params or {},
        json=_json,
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Store:
    """Minimal ConversationStorage stand-in for the sender-auth endpoint."""

    def __init__(self, *, channel, senders=()):
        self._channel = channel
        self._senders = [dict(s) for s in senders]

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

    # Unused by the sender-auth endpoint but present for create_channel paths.
    async def get_channel_by_type(self, profile, channel_type):
        return None

    async def create_channel(self, **kwargs):
        return {"id": "newch", "profile": kwargs.get("profile"), **kwargs}


def _channel(profile="p1"):
    return {"id": "ch1", "profile": profile, "channel_type": "telegram",
            "mode": "notification", "config": {}}


def _sender(sid="u1", authenticated=False):
    return {"id": f"row-{sid}", "sender_id": sid, "display_name": sid,
            "authenticated": authenticated, "pending_otp": "123456",
            "pending_otp_expires_at": None, "conversation_id": None}


# ── _validate_subscribe_auth ──────────────────────────────────────────────────

def test_validate_subscribe_auth_accepts_known_methods():
    for m in ("open", "passcode", "otp", "approval", "allowlist"):
        assert _validate_subscribe_auth({"subscribe_auth": m}) is None


def test_validate_subscribe_auth_allows_absent_or_empty():
    assert _validate_subscribe_auth({}) is None
    assert _validate_subscribe_auth({"subscribe_auth": ""}) is None


def test_validate_subscribe_auth_rejects_unknown():
    err = _validate_subscribe_auth({"subscribe_auth": "bogus"})
    assert err and "subscribe_auth must be one of" in err


# ── create_channel_for_profile rejects an invalid subscribe_auth ──────────────

def test_create_channel_rejects_invalid_subscribe_auth():
    store = _Store(channel=_channel())
    ch, err = asyncio.run(create_channel_for_profile(
        store, "p1",
        {
            "channel_type": "telegram", "mode": "notification", "enabled": False,
            "config": {"bot_token": "x", "subscribe_auth": "bogus"},
        },
    ))
    assert ch is None
    assert err and err["status"] == 400
    assert "subscribe_auth" in err["error"]


def test_create_bot_channel_rejects_invalid_subscribe_auth():
    # Validation now applies to conversational modes too, not just notification.
    store = _Store(channel=_channel())
    ch, err = asyncio.run(create_channel_for_profile(
        store, "p1",
        {
            "channel_type": "telegram", "mode": "bot", "enabled": False,
            "config": {"bot_token": "x", "subscribe_auth": "bogus"},
        },
    ))
    assert ch is None
    assert err and err["status"] == 400
    assert "subscribe_auth" in err["error"]


def test_create_bot_channel_accepts_approval():
    store = _Store(channel=_channel())
    ch, err = asyncio.run(create_channel_for_profile(
        store, "p1",
        {
            "channel_type": "telegram", "mode": "bot", "enabled": False,
            "config": {"bot_token": "x", "subscribe_auth": "approval"},
        },
    ))
    assert err is None and ch is not None
    assert ch["config"]["subscribe_auth"] == "approval"


# ── PATCH /senders/{id} approve / revoke ──────────────────────────────────────

_PATH = "/api/channels/{channel_id}/senders/{sender_id}"


def test_approve_sets_authenticated_and_clears_otp():
    store = _Store(channel=_channel(), senders=[_sender("u1", authenticated=False)])
    h = _handler(store, _PATH, "PATCH")
    resp = asyncio.run(h(_req(
        path_params={"channel_id": "ch1", "sender_id": "u1"},
        body={"authenticated": True},
    )))
    assert resp.status_code == 200
    sender = _body(resp)["sender"]
    assert sender["authenticated"] is True
    # pending_otp redacted in the response, and cleared in storage.
    assert store._senders[0]["pending_otp"] is None


def test_revoke_clears_authenticated():
    store = _Store(channel=_channel(), senders=[_sender("u1", authenticated=True)])
    h = _handler(store, _PATH, "PATCH")
    resp = asyncio.run(h(_req(
        path_params={"channel_id": "ch1", "sender_id": "u1"},
        body={"authenticated": False},
    )))
    assert resp.status_code == 200
    assert store._senders[0]["authenticated"] is False


def test_approve_missing_sender_is_404():
    store = _Store(channel=_channel(), senders=[])
    h = _handler(store, _PATH, "PATCH")
    resp = asyncio.run(h(_req(
        path_params={"channel_id": "ch1", "sender_id": "ghost"},
        body={"authenticated": True},
    )))
    assert resp.status_code == 404


def test_approve_wrong_profile_is_forbidden():
    store = _Store(channel=_channel(profile="owner"), senders=[_sender("u1")])
    h = _handler(store, _PATH, "PATCH")
    resp = asyncio.run(h(_req(
        username="intruder",
        path_params={"channel_id": "ch1", "sender_id": "u1"},
        body={"authenticated": True},
    )))
    assert resp.status_code == 403


def test_approve_requires_boolean_authenticated():
    store = _Store(channel=_channel(), senders=[_sender("u1")])
    h = _handler(store, _PATH, "PATCH")
    resp = asyncio.run(h(_req(
        path_params={"channel_id": "ch1", "sender_id": "u1"},
        body={"authenticated": "yes"},
    )))
    assert resp.status_code == 400
