"""What the userbot does with the rooms its account happens to be in.

A real Telegram account is a member of plenty of groups, and it receives every
message in all of them. The adapter no longer decides which of those are
interesting: every group message is handed to the channel-group pipeline, which
owns the approval question and answers it once per group. What the adapter still
decides is the DM/room split, the legacy-vs-supergroup label, and what never
reaches the pipeline at all — our own posts, empty messages, broadcast channels.

Drives the real ``TelegramUserbotAdapter._dispatch_event`` with hand-built
Telethon-shaped events; no Telethon, session file, or network is involved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.channels.adapters.telegram_userbot import TelegramUserbotAdapter

_CHAT_ID = -1001234
_ALEXA = 1644772063


class _Client:
    def __init__(self):
        self.sent: list[tuple] = []

    async def get_input_entity(self, value):
        return f"peer:{value}"

    async def send_message(self, peer, text, parse_mode=None):
        self.sent.append((peer, text, parse_mode))


class _Event:
    """A Telethon ``NewMessage`` event, reduced to what the adapter reads."""

    def __init__(
        self, *, is_private=False, is_group=False, is_channel=False,
        chat_id=_CHAT_ID, sender_id=_ALEXA, text="status?", out=False,
        message_id=42, sender=None, title="Ops room", date=1_700_000_000.0,
    ):
        self.is_private = is_private
        self.is_group = is_group
        self.is_channel = is_channel
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = SimpleNamespace(
            message=text, out=out, id=message_id,
            # Telethon exposes the platform's send time as a datetime.
            date=SimpleNamespace(timestamp=lambda: date) if date is not None else None,
        )
        self.chat = SimpleNamespace(title=title)
        self._sender = sender
        self.input_chats = 0

    async def get_sender(self):
        return self._sender

    async def get_input_chat(self):
        self.input_chats += 1
        return f"peer:{self.chat_id}"

    async def get_chat(self):
        return self.chat


def _sender(username="alexa", first="Alexa", last="Nguyen", bot=False):
    return SimpleNamespace(
        first_name=first, last_name=last, username=username, bot=bot,
    )


def _adapter(**state) -> TelegramUserbotAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "telegram",
        "mode": "userbot", "config": {}, "state": dict(state),
    }
    return TelegramUserbotAdapter(channel, storage=None)


def _capture(monkeypatch, adapter):
    group_calls: list[dict] = []
    dm_calls: list[tuple] = []

    async def _group(**kwargs):
        group_calls.append(kwargs)

    async def _dm(sender_id, display_name, text):
        dm_calls.append((sender_id, display_name, text))

    monkeypatch.setattr(adapter, "_handle_group_inbound", _group)
    monkeypatch.setattr(adapter, "_handle_inbound", _dm)
    return group_calls, dm_calls


def test_a_dm_still_takes_the_conversation_path(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(_Event(
            is_private=True, chat_id=_ALEXA, sender=_sender(), text="hello",
        ))

        assert group_calls == []
        assert dm_calls == [(str(_ALEXA), "Alexa Nguyen (@alexa)", "hello")]

    asyncio.run(scenario())


def test_a_room_message_goes_to_the_group(monkeypatch):
    """Every group this account is in is handed over — the pipeline, not the
    adapter, decides whether the group is one Cremind should answer in."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        event = _Event(is_group=True, is_channel=True, sender=_sender())
        await adapter._dispatch_event(event)

        assert dm_calls == []
        assert group_calls == [{
            "chat_id": str(_CHAT_ID),
            "chat_title": "Ops room",
            "chat_type": "supergroup",
            "sender_id": str(_ALEXA),
            "sender_username": "alexa",
            "display_name": "Alexa Nguyen (@alexa)",
            "text": "status?",
            "platform_message_id": "42",
            "platform_message_date": 1_700_000_000.0,
            "sender_is_bot": False,
            "mentioned": False,
        }]
        # The reply peer is cached under the ROOM, not the person who spoke.
        assert adapter._peer_cache[str(_CHAT_ID)] == f"peer:{_CHAT_ID}"

    asyncio.run(scenario())


def test_a_member_bots_mirror_is_reported_as_bot_authored(monkeypatch):
    """This is the only adapter that ever sees one, so the flag starts here."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(_Event(
            is_group=True, is_channel=True, sender_id=999,
            sender=_sender(username="catbot", first="Cat", last=None, bot=True),
        ))

        assert group_calls[0]["sender_is_bot"] is True

    asyncio.run(scenario())


def test_a_legacy_group_is_labelled_as_one(monkeypatch):
    """Telethon reports a supergroup as a channel too; a legacy group is not."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(
            _Event(is_group=True, is_channel=False, sender=_sender()),
        )

        assert group_calls[0]["chat_type"] == "group"

    asyncio.run(scenario())


def test_a_second_unrelated_room_is_handed_over_too(monkeypatch):
    """The regression guard for the rule that replaced binding: no group is
    filtered out here on the way in, however unfamiliar its chat id."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(_Event(
            is_group=True, is_channel=True, sender=_sender(),
            chat_id=-100999, title="Some other room",
        ))

        assert dm_calls == []
        assert [(c["chat_id"], c["chat_title"]) for c in group_calls] \
            == [("-100999", "Some other room")]

    asyncio.run(scenario())


def test_our_own_outgoing_message_is_skipped(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(
            _Event(is_group=True, is_channel=True, sender=_sender(), out=True),
        )

        assert group_calls == []

    asyncio.run(scenario())


def test_a_broadcast_channel_is_ignored_entirely(monkeypatch):
    """Nobody is talking in a broadcast channel — it is not a room."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(
            _Event(is_group=False, is_channel=True, sender=_sender()),
        )

        assert group_calls == []
        assert dm_calls == []               # and never mistaken for a DM either

    asyncio.run(scenario())


def test_an_empty_room_message_is_skipped(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_event(
            _Event(is_group=True, is_channel=True, sender=_sender(), text=""),
        )

        assert group_calls == []

    asyncio.run(scenario())


def test_send_to_chat_addresses_the_room():
    async def scenario():
        adapter = _adapter()
        adapter._client = _Client()
        await adapter.send_to_chat(str(_CHAT_ID), "mirrored")

        assert adapter._client.sent == [(f"peer:{_CHAT_ID}", "mirrored", "md")]

    asyncio.run(scenario())


# --- _is_mentioned -----------------------------------------------------------

def _mentionable_adapter() -> TelegramUserbotAdapter:
    return _adapter(self_identity={"user_id": "999", "username": "dogbot",
                                   "is_bot": False})


def test_telethons_own_mentioned_flag_is_believed():
    """Telethon precomputes this for both an ``@username`` and a reply to us,
    which is the whole question — the entity walk is only a fallback."""
    async def scenario():
        adapter = _mentionable_adapter()
        message = SimpleNamespace(message="@dogbot status?", mentioned=True)

        assert await adapter._is_mentioned(SimpleNamespace(), message) is True

    asyncio.run(scenario())


def test_an_inline_mention_entity_naming_us_counts_as_addressed():
    """The shape Telethon does NOT flag: an account with no username tagged by
    a ``MessageEntityMentionName`` carrying the user id."""
    async def scenario():
        adapter = _mentionable_adapter()
        message = SimpleNamespace(
            message="Dogbot status?", mentioned=False,
            entities=[SimpleNamespace(user_id=999, offset=0, length=6)],
        )

        assert await adapter._is_mentioned(SimpleNamespace(), message) is True

    asyncio.run(scenario())


def test_a_room_message_naming_nobody_is_not_addressed_to_us():
    async def scenario():
        adapter = _mentionable_adapter()
        message = SimpleNamespace(
            message="status?", mentioned=False, entities=None,
        )

        assert await adapter._is_mentioned(SimpleNamespace(), message) is False

    asyncio.run(scenario())
