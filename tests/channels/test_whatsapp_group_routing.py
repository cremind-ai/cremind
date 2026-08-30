"""Which path a sidecar frame takes: the room's, or the sender's DM.

WhatsApp group messages used to be dropped in the Node sidecar before any frame
was emitted, so a room could be bound and still never say anything. They now
arrive as their own ``incoming_group`` kind, and this file pins what the adapter
does with one: a room message is addressed to the room, and its sender is the
participant — reading the sender off the chat id, as the DM path does, would
collapse every human in the group into one identity.

It also pins the pairing frame. WhatsApp flags nothing as bot-authored: every
participant, our own mirror included, looks exactly like a person talking, so
the ids recorded at ``ready`` are the only thing that keeps the room from
answering itself.

Drives the real ``WhatsappAdapter._handle_sidecar_event`` with hand-built frames
— no Node process, no Baileys session, no pairing.
"""

from __future__ import annotations

import asyncio

from app.channels.adapters.whatsapp import WhatsappAdapter
from app.channels.groups.keys import _PER_ACCOUNT_MESSAGE_IDS

_ROOM = "120363041234567890@g.us"
_ALEXA_PN = "14155551212@s.whatsapp.net"
_ALEXA_LID = "77889900@lid"


class _Storage:
    """Records channel writes and echoes back the updated row, like the real one."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id, **kwargs):
        self.updates.append((channel_id, dict(kwargs)))
        return {
            "id": channel_id, "profile": "dog", "channel_type": "whatsapp",
            "mode": "userbot", "config": {}, "state": kwargs.get("state") or {},
        }


def _adapter(**state) -> WhatsappAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "whatsapp",
        "mode": "userbot", "config": {}, "state": dict(state),
    }
    return WhatsappAdapter(channel, _Storage())


def _group_frame(**overrides) -> dict:
    frame = {
        "kind": "incoming_group",
        "chat_id": _ROOM,
        "chat_title": "Ops room",
        "sender_id": _ALEXA_PN,
        "sender_alt_ids": [_ALEXA_LID],
        "display_name": "Alexa",
        "message_id": "3EB0A1B2C3",
        "timestamp": 1_700_000_000,
        "text": "status?",
    }
    frame.update(overrides)
    return frame


def _dm_frame(**overrides) -> dict:
    frame = {
        "kind": "incoming",
        "sender_id": _ALEXA_PN,
        "display_name": "Alexa",
        "text": "hello",
    }
    frame.update(overrides)
    return frame


async def _settle():
    # Inbound handling is spawned so one message never holds up the WebSocket
    # reader the send acks and pairing events share.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _capture(monkeypatch, adapter):
    """Replace both inbound paths so the routing decision is all that is tested."""
    group_calls: list[dict] = []
    dm_calls: list[tuple] = []

    async def _group(**kwargs):
        group_calls.append(kwargs)

    async def _dm(sender_id, display_name, text):
        dm_calls.append((sender_id, display_name, text))

    monkeypatch.setattr(adapter, "_handle_group_inbound", _group)
    monkeypatch.setattr(adapter, "_handle_inbound", _dm)
    return group_calls, dm_calls


def test_a_room_message_goes_to_the_group(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_group_frame())
        await _settle()

        assert dm_calls == []                       # never the per-sender path
        assert len(group_calls) == 1
        assert group_calls[0] == {
            "chat_id": _ROOM,
            "chat_title": "Ops room",
            "chat_type": "whatsapp_group",
            "sender_id": _ALEXA_PN,
            # WhatsApp has no usernames — a person is a number and a pushName.
            "sender_username": None,
            "sender_alt_ids": [_ALEXA_LID, "14155551212"],
            "display_name": "Alexa",
            "text": "status?",
            "platform_message_id": "3EB0A1B2C3",
            "platform_message_date": 1_700_000_000.0,
            "sender_is_bot": False,
            # Nobody tagged us in this one; the room decides what that means.
            "mentioned": False,
        }

    asyncio.run(scenario())


def test_the_chat_type_keeps_the_message_id_key(monkeypatch):
    """Calling a WhatsApp room a "group" would opt it into the per-account
    fingerprint legacy Telegram groups need, throwing away a message id that
    is the same on every account that received the message."""
    async def scenario():
        adapter = _adapter()
        group_calls, _ = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_group_frame())
        await _settle()

        assert ("whatsapp", group_calls[0]["chat_type"]) not in _PER_ACCOUNT_MESSAGE_IDS

    asyncio.run(scenario())


def test_the_phone_digits_are_offered_as_an_alternate_id(monkeypatch):
    """A JID is not something anyone can look up; the number on the contact
    card is what an operator pastes into User accounts."""
    async def scenario():
        adapter = _adapter()
        group_calls, _ = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_group_frame(sender_alt_ids=[]))
        await _settle()

        assert group_calls[0]["sender_alt_ids"] == ["14155551212"]

    asyncio.run(scenario())


def test_a_lid_only_participant_still_routes(monkeypatch):
    """Some accounts are only ever reported under the opaque ``@lid`` form,
    which carries no number to derive — they must still reach the room."""
    async def scenario():
        adapter = _adapter()
        group_calls, _ = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(
            _group_frame(sender_id=_ALEXA_LID, sender_alt_ids=[]),
        )
        await _settle()

        assert group_calls[0]["sender_id"] == _ALEXA_LID
        assert group_calls[0]["sender_alt_ids"] == []

    asyncio.run(scenario())


def test_a_room_with_no_title_yet_still_routes(monkeypatch):
    """The subject lookup is a background round trip in the sidecar, so the
    first message from a room carries no title."""
    async def scenario():
        adapter = _adapter()
        group_calls, _ = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(
            _group_frame(chat_title=None, display_name=None, message_id=None,
                         timestamp=None),
        )
        await _settle()

        assert group_calls[0]["chat_title"] is None
        assert group_calls[0]["display_name"] is None
        assert group_calls[0]["platform_message_id"] is None
        assert group_calls[0]["platform_message_date"] is None

    asyncio.run(scenario())


def test_an_incomplete_room_frame_is_ignored(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_group_frame(text=""))
        await adapter._handle_sidecar_event(_group_frame(chat_id=""))
        await adapter._handle_sidecar_event(_group_frame(sender_id=""))
        await _settle()

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_direct_message_still_takes_the_dm_path(monkeypatch):
    """The regression guard: nothing about 1:1 chats changed."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_dm_frame())
        await _settle()

        assert group_calls == []
        assert dm_calls == [(_ALEXA_PN, "Alexa", "hello")]

    asyncio.run(scenario())


def test_pairing_records_both_forms_of_our_own_id():
    """Our mirror comes back from a device that may report either form; a
    single-id echo check misses it and the room starts answering itself."""
    async def scenario():
        adapter = _adapter()

        await adapter._handle_sidecar_event({
            "kind": "ready", "self_id": "14155550000", "self_lid": "5566@lid",
        })

        assert adapter.channel["state"]["self_identity"] == {
            "user_id": "14155550000",
            "username": None,
            "is_bot": False,
            "mention": "@14155550000",
            "alt_ids": ["5566@lid", "14155550000@s.whatsapp.net"],
        }
        assert adapter.storage.updates[0][0] == "c1"

    asyncio.run(scenario())


def test_a_bare_lid_is_stored_in_full_jid_form():
    """Baileys has reported the lid both bare and suffixed; the two forms must
    not both end up in the index, where the comparison is exact."""
    async def scenario():
        adapter = _adapter()

        await adapter._handle_sidecar_event({
            "kind": "ready", "self_id": "14155550000", "self_lid": "5566",
        })

        assert adapter.channel["state"]["self_identity"]["alt_ids"][0] == "5566@lid"

    asyncio.run(scenario())


def test_pairing_without_an_identity_is_not_fatal():
    """An older sidecar reports no ids at all — the channel must still pair and
    carry DMs, only the room's echo filter goes blind."""
    async def scenario():
        adapter = _adapter()

        await adapter._handle_sidecar_event({"kind": "ready"})

        assert "self_identity" not in adapter.channel["state"]

    asyncio.run(scenario())


def test_send_to_chat_addresses_the_room(monkeypatch):
    """A room's ``@g.us`` id is nobody's sender id, and the send has to stay on
    the acked path so a rejected mirror surfaces instead of vanishing."""
    async def scenario():
        adapter = _adapter()
        sent: list[tuple[str, str]] = []

        async def _send(sender_id, text):
            sent.append((sender_id, text))

        monkeypatch.setattr(adapter, "_send_text", _send)
        await adapter.send_to_chat(_ROOM, "mirrored")

        assert sent == [(_ROOM, "mirrored")]

    asyncio.run(scenario())


def test_the_adapter_advertises_room_support():
    """Nothing else lists a platform for binding — the class attribute is what
    puts WhatsApp on /api/group-chats/channel-types and past the API's 400."""
    assert WhatsappAdapter.supports_group_chats is True
    # WhatsApp really does spell emphasis *bold* / _italic_, so the mirror's
    # defaults travel unchanged; a wrong override here would put literal
    # asterisks in every room message.
    assert WhatsappAdapter.bold_markup == ("*", "*")
    assert WhatsappAdapter.italic_markup == ("_", "_")


# --- _is_mentioned -----------------------------------------------------------
#
# WhatsApp carries a ping and a quote as annotations, never in the text, so the
# sidecar extracts them and the adapter compares them against EVERY id we are
# known by: which form a mention names depends on the sender's client, and a
# comparison against one of them misses the other two.

def _mentionable_adapter() -> WhatsappAdapter:
    return _adapter(self_identity={
        "user_id": "14155550000",
        "username": None,
        "is_bot": False,
        "mention": "@14155550000",
        "alt_ids": ["5566@lid", "14155550000@s.whatsapp.net"],
    })


def test_a_ping_naming_our_jid_counts_as_addressed():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned(
        {"mentioned_ids": ["14155550000@s.whatsapp.net"]},
    ) is True


def test_a_ping_naming_our_lid_form_counts_too():
    """The same account, reported from a different device."""
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"mentioned_ids": ["5566@lid"]}) is True


def test_a_ping_naming_only_our_digits_counts_too():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"mentioned_ids": ["14155550000"]}) is True


def test_a_quote_of_one_of_our_own_messages_counts_as_addressed():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"quoted_sender_id": _ALEXA_PN}) is False
    assert adapter._is_mentioned(
        {"quoted_sender_id": "14155550000@s.whatsapp.net"},
    ) is True


def test_a_ping_naming_somebody_else_is_not_addressed_to_us():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"mentioned_ids": [_ALEXA_PN, _ALEXA_LID]}) is False


def test_a_frame_with_no_annotations_is_not_addressed_to_us():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({}) is False


def test_before_pairing_nothing_reads_as_addressed():
    """No identity recorded yet — there is nothing to match, and claiming a
    mention would wake the agent on every message in the room."""
    adapter = _adapter()

    assert adapter._is_mentioned({"mentioned_ids": ["14155550000"]}) is False
