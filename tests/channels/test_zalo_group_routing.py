"""Which path a polled Zalo Bot API update takes: the room's, or a person's DM.

The bug this file exists for: ``_handle_update`` read no chat type at all and
handed the CHAT id to the DM pipeline as if it were a sender. A message written
in a group therefore opened one conversation keyed on the room — everybody in it
collapsed into a single sender — and the agent answered the room as though it
were a person.

Drives the real ``ZaloBotAdapter._handle_update`` with hand-built update dicts
(the Zalo client returns plain JSON), so no token, bot, or network is involved.
"""

from __future__ import annotations

import asyncio

from app.channels.adapters.zalo import ZaloBotAdapter

_CHAT_ID = "grp-98765"
_SENDER_ID = "user-1644772063"


class _Storage:
    """Records channel writes and echoes back the updated row, like the real one."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id, **kwargs):
        self.updates.append((channel_id, dict(kwargs)))
        return {
            "id": channel_id, "profile": "dog", "channel_type": "zalo",
            "mode": "bot", "config": {}, "state": kwargs.get("state") or {},
        }


def _adapter(**state) -> ZaloBotAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "zalo",
        "mode": "bot", "config": {"bot_token": "1:2"}, "state": dict(state),
    }
    return ZaloBotAdapter(channel, _Storage())


def _sender(sender_id=_SENDER_ID, display_name="Alexa Nguyen", is_bot=False):
    return {"id": sender_id, "display_name": display_name, "is_bot": is_bot}


def _update(chat_type="GROUP", *, chat_id=_CHAT_ID, text="status?",
            message_id="m-42", date=1_700_000_000, sender=None,
            title="Ops room"):
    chat = {"id": chat_id, "chat_type": chat_type}
    if title is not None:
        chat["title"] = title
    return {
        "event_name": "message.text.received",
        "message": {
            "message_id": message_id,
            "text": text,
            "date": date,
            "chat": chat,
            "from": _sender() if sender is None else sender,
        },
    }


async def _settle():
    # ``_handle_update`` spawns per-message tasks so the poll loop never blocks.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _capture(monkeypatch, adapter):
    """Replace both inbound paths so the routing decision is all that is tested."""
    group_calls: list[dict] = []
    dm_calls: list[tuple] = []

    async def _group(**kwargs):
        group_calls.append(kwargs)

    async def _dm(sender_id, display_name, text, files=None):
        dm_calls.append((sender_id, display_name, text))

    monkeypatch.setattr(adapter, "_handle_group_inbound", _group)
    monkeypatch.setattr(adapter, "_handle_inbound", _dm)
    return group_calls, dm_calls


def test_a_group_message_goes_to_the_room(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        adapter._handle_update(_update())
        await _settle()

        assert dm_calls == []                       # never the private path
        assert group_calls == [{
            "chat_id": _CHAT_ID,
            "chat_title": "Ops room",
            "chat_type": "group",
            "sender_id": _SENDER_ID,
            "sender_username": None,
            "display_name": "Alexa Nguyen",
            "text": "status?",
            "platform_message_id": "m-42",
            "platform_message_date": 1_700_000_000.0,
            "sender_is_bot": False,
            "files": None,
        }]

    asyncio.run(scenario())


def test_a_supergroup_is_a_room_too(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        adapter._handle_update(_update("SuperGroup"))
        await _settle()

        assert dm_calls == []
        assert group_calls[0]["chat_type"] == "supergroup"

    asyncio.run(scenario())


def test_a_private_chat_still_keys_the_dm_on_the_chat_id(monkeypatch):
    """The regression guard: a DM's conversation is keyed on the chat id, which
    is the id ``sendMessage`` wants back — not on the sender id."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        adapter._handle_update(
            _update("PRIVATE", chat_id="chat-7", text="hello", title=None),
        )
        await _settle()

        assert group_calls == []
        assert dm_calls == [("chat-7", "Alexa Nguyen", "hello")]

    asyncio.run(scenario())


def test_a_chat_that_declares_no_type_stays_a_dm(monkeypatch):
    """Routing is on the declared type alone: a Zalo chat id is opaque, so a
    room's is indistinguishable from a person's and guessing would eventually
    answer a room privately."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        update = _update(title=None)
        del update["message"]["chat"]["chat_type"]
        adapter._handle_update(update)
        await _settle()

        assert group_calls == []
        assert dm_calls == [(_CHAT_ID, "Alexa Nguyen", "status?")]

    asyncio.run(scenario())


def test_a_room_that_reports_a_name_instead_of_a_title_is_still_named(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        update = _update(title=None)
        update["message"]["chat"]["name"] = "Ops room"
        adapter._handle_update(update)
        await _settle()

        assert group_calls[0]["chat_title"] == "Ops room"

    asyncio.run(scenario())


def test_an_authorless_group_message_is_dropped(monkeypatch):
    """Nothing to attribute it to, and the room is not a sender."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        adapter._handle_update(_update(sender={"display_name": "System"}))
        await _settle()

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_bot_author_is_reported_not_hidden(monkeypatch):
    """The group layer decides what to do with it; the adapter must not lie."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        adapter._handle_update(_update(
            sender=_sender(sender_id="bot-9", display_name="Cat", is_bot=True),
        ))
        await _settle()

        assert group_calls[0]["sender_is_bot"] is True

    asyncio.run(scenario())


def test_a_message_with_no_usable_timestamp_still_routes(monkeypatch):
    """The send time only sharpens the dedupe key; losing it must not lose the
    message, and it must never raise inside the poll loop."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        adapter._handle_update(_update(date=None))
        adapter._handle_update(_update(date="not-a-time", message_id="m-43"))
        await _settle()

        assert [call["platform_message_date"] for call in group_calls] == [None, None]

    asyncio.run(scenario())


def test_send_to_chat_addresses_the_room():
    """A room's chat id is nobody's sender id, and Zalo cuts anything past 2000
    characters, so a mirrored bubble has to be split before it is sent."""
    async def scenario():
        adapter = _adapter()
        sent: list[tuple[str, str]] = []

        class _Api:
            async def send_message(self, chat_id, text):
                sent.append((chat_id, text))

        adapter._api = _Api()
        await adapter.send_to_chat(_CHAT_ID, "x" * 4500)

        assert [chat_id for chat_id, _ in sent] == [_CHAT_ID] * len(sent)
        assert len(sent) >= 3
        assert all(len(text) <= 2000 for _, text in sent)

    asyncio.run(scenario())


def test_get_me_records_which_bot_we_are():
    """``call`` already unwraps the envelope's ``result``, so this is the bot."""
    async def scenario():
        adapter = _adapter()
        await adapter._store_self_from_get_me(
            {"id": 777, "account_name": "cremind-bot"},
        )

        assert adapter.channel["state"]["self_identity"] == {
            "user_id": "777", "username": "cremind-bot", "is_bot": True,
        }
        assert adapter.storage.updates[0][0] == "c1"

    asyncio.run(scenario())


def test_an_unrecognisable_get_me_is_not_worth_refusing_to_poll():
    async def scenario():
        adapter = _adapter()
        await adapter._store_self_from_get_me({"ok": True})
        await adapter._store_self_from_get_me(None)

        assert "self_identity" not in adapter.channel["state"]
        assert adapter.storage.updates == []

    asyncio.run(scenario())


def test_the_room_is_never_sent_markup():
    """Zalo renders text literally, so the mirror's emphasis has to disappear
    rather than arrive as stray asterisks around somebody's name."""
    adapter = _adapter()

    assert adapter.bold("Ana") == "Ana"
    assert adapter.italic("Thought") == "Thought"
