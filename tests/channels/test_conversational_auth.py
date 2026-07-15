"""Conversational (bot/userbot) access-auth, unified with notification mode.

Drives the real ``BaseChannelAdapter._handle_inbound`` gate with a fake storage
and a recording ``_dispatch_to_agent`` (so nothing is enqueued to the agent).
Covers every method (open/passcode/otp/approval/allowlist) plus the legacy
``auth_mode`` / ``config.password`` back-compat that ``_subscribe_auth`` resolves.
"""

from __future__ import annotations

import asyncio

import pytest

import app.channels.base as base_mod
from app.channels.base import BaseChannelAdapter


class _Storage:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._n = 0
        self._c = 0

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None):
        r = self.rows.get(sender_id)
        if r is None:
            self._n += 1
            r = {
                "id": f"s{self._n}", "channel_id": channel_id, "sender_id": sender_id,
                "display_name": display_name, "authenticated": False,
                "pending_otp": None, "pending_otp_expires_at": None,
                "conversation_id": None,
            }
            self.rows[sender_id] = r
        elif display_name:
            r["display_name"] = display_name
        return dict(r)

    async def update_sender(self, row_id, **fields):
        for r in self.rows.values():
            if r["id"] == row_id:
                r.update(fields)
                return dict(r)
        return None

    async def list_senders(self, channel_id):
        return [dict(r) for r in self.rows.values()]

    async def get_conversation(self, conv_id):
        return None

    async def create_conversation(self, profile, title, channel_id):
        self._c += 1
        return {"id": f"conv{self._c}"}


class _ConvAdapter(BaseChannelAdapter):
    def __init__(self, channel, storage):
        super().__init__(channel, storage)
        self.sent: list[tuple[str, str]] = []
        self.dispatched: list[tuple] = []

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append((sender_id, text))

    async def _dispatch_to_agent(self, conversation_id, sender_id, display_name, text):
        self.dispatched.append((conversation_id, sender_id, text))


@pytest.fixture(autouse=True)
def _capture_pushes(monkeypatch):
    """Capture operator notifications (channel_otp / access request) pushed from base."""
    pushes: list[dict] = []

    class _Fake:
        def push(self, **kwargs):
            pushes.append(kwargs)

    monkeypatch.setattr(base_mod, "get_event_notifications", lambda: _Fake())
    return pushes


def _adapter(*, subscribe_auth=None, passcode=None, auth_mode=None, password=None, mode="bot"):
    config: dict = {}
    if subscribe_auth is not None:
        config["subscribe_auth"] = subscribe_auth
    if passcode is not None:
        config["subscribe_passcode"] = passcode
    if password is not None:
        config["password"] = password
    channel = {
        "id": "c1", "profile": "admin", "channel_type": "telegram",
        "mode": mode, "config": config,
    }
    if auth_mode is not None:
        channel["auth_mode"] = auth_mode
    return _ConvAdapter(channel, _Storage())


def _msg(a, sender_id, text, name="Tester"):
    asyncio.run(a._handle_inbound(sender_id, name, text))


def _authed(a, sender_id):
    return a.storage.rows.get(sender_id, {}).get("authenticated") is True


# open -------------------------------------------------------------------------

def test_open_dispatches_immediately():
    a = _adapter(subscribe_auth="open")
    _msg(a, "u1", "hello")
    assert len(a.dispatched) == 1
    assert a.sent == []  # no gate message


def test_unset_defaults_to_open():
    a = _adapter()  # nothing set
    _msg(a, "u1", "hello")
    assert len(a.dispatched) == 1


# passcode ---------------------------------------------------------------------

def test_passcode_gates_then_unlocks():
    a = _adapter(subscribe_auth="passcode", passcode="s3cret")
    _msg(a, "u1", "hello")          # wrong (no passcode)
    assert a.dispatched == [] and not _authed(a, "u1")
    _msg(a, "u1", "s3cret")         # correct → authenticated, not dispatched
    assert _authed(a, "u1") and a.dispatched == []
    _msg(a, "u1", "now chatting")   # authenticated → dispatched
    assert len(a.dispatched) == 1


# otp --------------------------------------------------------------------------

def test_otp_challenge_then_chat(_capture_pushes):
    a = _adapter(subscribe_auth="otp")
    _msg(a, "u1", "hello")          # challenge issued
    assert a.dispatched == []
    code = a.storage.rows["u1"]["pending_otp"]
    assert code and any(p.get("kind") == "channel_otp" for p in _capture_pushes)
    _msg(a, "u1", code)             # correct code → authenticated, not dispatched
    assert _authed(a, "u1") and a.dispatched == []
    _msg(a, "u1", "hi")             # now dispatches
    assert len(a.dispatched) == 1


# approval ---------------------------------------------------------------------

def test_approval_holds_then_dispatches_after_approve(_capture_pushes):
    a = _adapter(subscribe_auth="approval")
    _msg(a, "u1", "hello")
    assert a.dispatched == [] and not _authed(a, "u1")
    assert any(p.get("kind") == "channel_subscribe_request" for p in _capture_pushes)
    assert any("pending" in t.lower() for _, t in a.sent)
    # second message while pending: still held, operator NOT re-notified
    before = len([p for p in _capture_pushes if p.get("kind") == "channel_subscribe_request"])
    _msg(a, "u1", "still there?")
    after = len([p for p in _capture_pushes if p.get("kind") == "channel_subscribe_request"])
    assert a.dispatched == [] and after == before  # deduped
    # operator approves
    asyncio.run(a.storage.update_sender(a.storage.rows["u1"]["id"], authenticated=True))
    _msg(a, "u1", "hi now")
    assert len(a.dispatched) == 1


# allowlist --------------------------------------------------------------------

def test_allowlist_refuses_then_dispatches_after_approve(_capture_pushes):
    a = _adapter(subscribe_auth="allowlist")
    _msg(a, "u1", "hello")
    assert a.dispatched == [] and not _authed(a, "u1")
    assert any("not authorized" in t.lower() for _, t in a.sent)
    # allowlist is silent — no operator request notification
    assert not any(p.get("kind") == "channel_subscribe_request" for p in _capture_pushes)
    # but a row exists so the operator can approve
    asyncio.run(a.storage.update_sender(a.storage.rows["u1"]["id"], authenticated=True))
    _msg(a, "u1", "hi now")
    assert len(a.dispatched) == 1


# legacy back-compat -----------------------------------------------------------

def test_legacy_auth_mode_password_still_gates():
    a = _adapter(auth_mode="password", password="s3cret")
    assert a._subscribe_auth() == "passcode"
    _msg(a, "u1", "wrong")
    assert a.dispatched == []
    _msg(a, "u1", "s3cret")
    assert _authed(a, "u1")


def test_legacy_auth_mode_otp_maps_to_otp():
    a = _adapter(auth_mode="otp")
    assert a._subscribe_auth() == "otp"
    _msg(a, "u1", "hello")
    assert a.storage.rows["u1"]["pending_otp"] and a.dispatched == []


def test_legacy_auth_mode_none_is_open():
    a = _adapter(auth_mode="none")
    assert a._subscribe_auth() == "open"
    _msg(a, "u1", "hello")
    assert len(a.dispatched) == 1


def test_legacy_config_password_only_infers_passcode():
    a = _adapter(password="s3cret")  # no subscribe_auth, no auth_mode
    assert a._subscribe_auth() == "passcode"
    _msg(a, "u1", "s3cret")
    assert _authed(a, "u1")
