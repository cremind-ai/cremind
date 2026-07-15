"""Tests for the notification-mode outbound delivery helpers.

Covers the programmatic push path added for the ``send_notification`` tool:

- :meth:`NotificationDeliveryMixin.deliver_text` — resolve recipients (static
  ``target_chat_ids`` ∪ authenticated subscribers) and fan out raw text.
- :meth:`ChannelRegistry.notification_adapters_for_profile` /
  :func:`has_notification_channel` — the sync, in-memory availability gate.
"""

from __future__ import annotations

import asyncio

from app.channels.notification_delivery import NotificationDeliveryMixin


class _FakeStorage:
    def __init__(self, senders):
        self._senders = senders

    async def list_senders(self, channel_id):
        return list(self._senders)


class _FakeAdapter(NotificationDeliveryMixin):
    """Minimal mixin host — captures ``_send_chunked`` calls instead of sending."""

    def __init__(self, channel, storage):
        self.channel = channel
        self.storage = storage
        self.channel_id = channel["id"]
        self.channel_type = channel["channel_type"]
        self.sent: list[tuple[str, str]] = []

    async def _send_chunked(self, target, text):
        self.sent.append((target, text))


def _adapter(*, target_chat_ids=None, senders=()):
    channel = {
        "id": "c1",
        "channel_type": "telegram",
        "mode": "notification",
        "config": {"target_chat_ids": target_chat_ids} if target_chat_ids else {},
    }
    return _FakeAdapter(channel, _FakeStorage(list(senders)))


# ── deliver_text ────────────────────────────────────────────────────────────


def test_deliver_text_union_of_targets_and_subscribers():
    adapter = _adapter(
        target_chat_ids="111,222",
        senders=[
            {"sender_id": "333", "authenticated": True},
            {"sender_id": "444", "authenticated": False},  # not subscribed
            {"sender_id": "222", "authenticated": True},  # dup of a static target
        ],
    )
    count = asyncio.run(adapter.deliver_text("hello"))
    assert count == 3  # 111, 222, 333 — 444 excluded, 222 deduped
    assert {t for t, _ in adapter.sent} == {"111", "222", "333"}
    assert all(text == "hello" for _, text in adapter.sent)


def test_deliver_text_empty_text_sends_nothing():
    adapter = _adapter(target_chat_ids="111")
    assert asyncio.run(adapter.deliver_text("   ")) == 0
    assert adapter.sent == []


def test_deliver_text_no_recipients_returns_zero():
    adapter = _adapter()  # no target_chat_ids, no subscribers
    assert asyncio.run(adapter.deliver_text("hi")) == 0
    assert adapter.sent == []


def test_deliver_text_propagates_transport_error():
    adapter = _adapter(target_chat_ids="111")

    async def _boom(target, text):
        raise RuntimeError("transport down")

    adapter._send_chunked = _boom  # type: ignore[assignment]
    try:
        asyncio.run(adapter.deliver_text("hi"))
    except RuntimeError as exc:
        assert "transport down" in str(exc)
    else:  # pragma: no cover - the point is that it raises
        raise AssertionError("deliver_text should propagate transport errors")


# ── registry gate helpers ─────────────────────────────────────────────────────


class _RegAdapter:
    def __init__(self, profile, mode, channel_type="telegram"):
        self.profile = profile
        self.channel_type = channel_type
        self.channel_id = f"{profile}-{channel_type}"
        self._mode = mode

    def _is_notification_mode(self):
        return self._mode == "notification"


def test_notification_adapters_for_profile_filters_by_profile_and_mode():
    from app.channels.registry import ChannelRegistry

    reg = ChannelRegistry(storage=None)
    reg._adapters = {
        "a": _RegAdapter("alice", "notification"),
        "b": _RegAdapter("alice", "bot"),  # wrong mode
        "c": _RegAdapter("bob", "notification"),  # wrong profile
    }
    got = reg.notification_adapters_for_profile("alice")
    assert [a.channel_id for a in got] == ["alice-telegram"]


def test_has_notification_channel_false_when_registry_uninitialized(monkeypatch):
    import app.channels.registry as reg

    monkeypatch.setattr(reg, "_instance", None)
    assert reg.has_notification_channel("alice") is False


def test_has_notification_channel_reflects_live_adapters(monkeypatch):
    import app.channels.registry as reg

    r = reg.ChannelRegistry(storage=None)
    r._adapters = {"a": _RegAdapter("alice", "notification")}
    monkeypatch.setattr(reg, "_instance", r)
    assert reg.has_notification_channel("alice") is True
    assert reg.has_notification_channel("bob") is False


# ── subscription authentication ───────────────────────────────────────────────


class _SubStorage:
    """Mutable sender store backing the subscribe-auth flows."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._n = 0

    async def get_or_create_sender(self, channel_id, sender_id, display_name=None):
        row = self.rows.get(sender_id)
        if row is None:
            self._n += 1
            row = {
                "id": f"s{self._n}", "channel_id": channel_id, "sender_id": sender_id,
                "display_name": display_name, "authenticated": False,
                "pending_otp": None, "pending_otp_expires_at": None,
                "conversation_id": None,
            }
            self.rows[sender_id] = row
        elif display_name:
            row["display_name"] = display_name
        return dict(row)

    async def update_sender(self, sender_row_id, **fields):
        for row in self.rows.values():
            if row["id"] == sender_row_id:
                row.update(fields)
                return dict(row)
        return None

    async def list_senders(self, channel_id):
        return [dict(r) for r in self.rows.values()]


class _SubAdapter(NotificationDeliveryMixin):
    def __init__(self, channel, storage):
        self.channel = channel
        self.storage = storage
        self.channel_id = channel["id"]
        self.channel_type = channel["channel_type"]
        self.profile = channel.get("profile", "admin")
        self.sent: list[tuple[str, str]] = []
        self.chunked: list[tuple[str, str]] = []
        self.pushed: list[dict] = []

    async def send(self, sender_id, text):
        self.sent.append((sender_id, text))

    async def _send_chunked(self, target, text):
        self.chunked.append((target, text))

    def _push_operator_notification(self, **kwargs):  # capture instead of pushing
        self.pushed.append(kwargs)


def _sub_adapter(*, subscribe_auth=None, passcode=None, target_chat_ids=None):
    config: dict = {}
    if subscribe_auth is not None:
        config["subscribe_auth"] = subscribe_auth
    if passcode is not None:
        config["subscribe_passcode"] = passcode
    if target_chat_ids is not None:
        config["target_chat_ids"] = target_chat_ids
    channel = {
        "id": "c1", "channel_type": "telegram", "mode": "notification",
        "profile": "admin", "config": config,
    }
    return _SubAdapter(channel, _SubStorage())


def _cmd(adapter, sender_id, text, display_name="Tester"):
    return asyncio.run(
        adapter._handle_notification_command(sender_id, display_name, text),
    )


def _authed(adapter, sender_id):
    return adapter.storage.rows.get(sender_id, {}).get("authenticated") is True


# open (default) ---------------------------------------------------------------

def test_subscribe_auth_defaults_to_open():
    assert _sub_adapter()._subscribe_auth() == "open"


def test_open_subscribes_on_start():
    a = _sub_adapter(subscribe_auth="open")
    _cmd(a, "u1", "/start")
    assert _authed(a, "u1")
    assert any("Subscribed" in t for _, t in a.sent)


def test_open_is_default_when_unset():
    a = _sub_adapter()  # no subscribe_auth, no passcode
    _cmd(a, "u1", "/start")
    assert _authed(a, "u1")


# passcode ---------------------------------------------------------------------

def test_passcode_wrong_is_refused():
    a = _sub_adapter(subscribe_auth="passcode", passcode="s3cret")
    _cmd(a, "u1", "/start nope")
    assert not _authed(a, "u1")
    assert any("passcode" in t.lower() for _, t in a.sent)


def test_passcode_correct_subscribes():
    a = _sub_adapter(subscribe_auth="passcode", passcode="s3cret")
    _cmd(a, "u1", "/start s3cret")
    assert _authed(a, "u1")


def test_passcode_backcompat_infers_from_config():
    # subscribe_auth unset but a passcode is configured → behaves as passcode.
    a = _sub_adapter(passcode="s3cret")
    assert a._subscribe_auth() == "passcode"
    _cmd(a, "u1", "/start")  # no passcode arg
    assert not _authed(a, "u1")
    _cmd(a, "u1", "/start s3cret")
    assert _authed(a, "u1")


# allowlist --------------------------------------------------------------------

def test_allowlist_refuses_self_subscribe():
    a = _sub_adapter(subscribe_auth="allowlist", target_chat_ids="111")
    _cmd(a, "u1", "/start")
    assert not _authed(a, "u1")
    assert any("disabled" in t.lower() for _, t in a.sent)


def test_allowlist_recipients_are_targets_only():
    a = _sub_adapter(subscribe_auth="allowlist", target_chat_ids="111,222")
    # Even a leftover authenticated subscriber must be excluded in allowlist mode.
    asyncio.run(a.storage.get_or_create_sender("c1", "333"))
    a.storage.rows["333"]["authenticated"] = True
    recipients = asyncio.run(a._notification_recipients())
    assert recipients == ["111", "222"]


# approval ---------------------------------------------------------------------

def test_approval_leaves_pending_and_notifies_operator():
    a = _sub_adapter(subscribe_auth="approval")
    _cmd(a, "u1", "/start")
    assert not _authed(a, "u1")  # pending, not subscribed
    assert any(p.get("kind") == "channel_subscribe_request" for p in a.pushed)
    # Not yet a recipient.
    assert asyncio.run(a._notification_recipients()) == []


def test_approval_then_manual_approve_becomes_recipient():
    a = _sub_adapter(subscribe_auth="approval")
    _cmd(a, "u1", "/start")
    # Operator approves (what the PATCH endpoint / CLI does).
    row = a.storage.rows["u1"]
    asyncio.run(a.storage.update_sender(row["id"], authenticated=True))
    assert asyncio.run(a._notification_recipients()) == ["u1"]


# otp --------------------------------------------------------------------------

def test_otp_challenge_then_correct_code_subscribes():
    a = _sub_adapter(subscribe_auth="otp")
    _cmd(a, "u1", "/start")
    assert not _authed(a, "u1")
    code = a.storage.rows["u1"]["pending_otp"]
    assert code and len(code) == 6
    assert any(p.get("kind") == "channel_otp" for p in a.pushed)
    # Subscriber echoes the code (a non-command message).
    _cmd(a, "u1", code)
    assert _authed(a, "u1")
    assert a.storage.rows["u1"]["pending_otp"] is None


def test_otp_wrong_code_stays_pending():
    a = _sub_adapter(subscribe_auth="otp")
    _cmd(a, "u1", "/start")
    _cmd(a, "u1", "000000" if a.storage.rows["u1"]["pending_otp"] != "000000" else "111111")
    assert not _authed(a, "u1")
    assert any("incorrect" in t.lower() for _, t in a.sent)


def test_otp_expired_code_reissues():
    a = _sub_adapter(subscribe_auth="otp")
    _cmd(a, "u1", "/start")
    old = a.storage.rows["u1"]["pending_otp"]
    a.storage.rows["u1"]["pending_otp_expires_at"] = 1.0  # far in the past
    _cmd(a, "u1", old)
    assert not _authed(a, "u1")
    # A fresh code was issued (different from the expired one).
    assert a.storage.rows["u1"]["pending_otp"] not in (None, old)


# unsubscribe ------------------------------------------------------------------

def test_stop_unsubscribes_regardless_of_method():
    a = _sub_adapter(subscribe_auth="open")
    _cmd(a, "u1", "/start")
    assert _authed(a, "u1")
    _cmd(a, "u1", "/stop")
    assert not _authed(a, "u1")
