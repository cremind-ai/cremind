"""A sidecar file send that is never acknowledged is UNCONFIRMED, not failed.

The WhatsApp and Zalo sidecars take a file send as a frame and answer it once
the platform has the upload. When that answer never comes, the frame was
written and the upload may well have landed — so the adapters raise
:class:`DeliveryUnconfirmed`, which the chat-file sender reports as "may or may
not have arrived" and refuses to repeat, instead of a plain failure a caller
would retry into a duplicate. A refusal the sidecar DOES report stays an
ordinary error. Both are still ``ChannelAuthError`` for the callers that caught
ack timeouts as one.

Also here: which address each adapter hands the sidecar for a room, and the
room shapes ``send_channel_message`` refuses.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.channels.adapters import whatsapp as wa_mod
from app.channels.adapters import zalo_userbot as zu_mod
from app.channels.adapters.slack import SlackAdapter
from app.channels.adapters.telegram import TelegramAdapter
from app.channels.adapters.telegram_userbot import TelegramUserbotAdapter
from app.channels.adapters.whatsapp import WhatsappAdapter
from app.channels.adapters.zalo_userbot import ZaloUserbotAdapter
from app.channels.exceptions import ChannelAuthError, DeliveryUnconfirmed


class _Socket:
    """A sidecar WebSocket that answers file frames as scripted."""

    def __init__(self, adapter, reply):
        self.adapter = adapter
        self.reply = reply  # None = never answer; else the ack's extra fields
        self.frames: list[dict] = []

    async def send(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        if self.reply is not None:
            self.adapter._resolve_pending({"request_id": frame["request_id"], **self.reply})


def _whatsapp(reply):
    adapter = WhatsappAdapter(
        {"id": "wa1", "profile": "p", "channel_type": "whatsapp", "mode": "userbot",
         "config": {}, "state": {}},
        storage=None,
    )
    adapter._ws = _Socket(adapter, reply)
    return adapter


def _zalo(reply):
    adapter = ZaloUserbotAdapter(
        {"id": "zl1", "profile": "p", "channel_type": "zalo", "mode": "userbot",
         "config": {}, "state": {}},
        storage=None,
    )
    adapter._ws = _Socket(adapter, reply)
    return adapter


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7 report")
    return str(path)


@pytest.fixture(autouse=True)
def _quick_acks(monkeypatch):
    monkeypatch.setattr(wa_mod, "_FILE_ACK_TIMEOUT", 0.05)
    monkeypatch.setattr(zu_mod, "_FILE_ACK_TIMEOUT", 0.05)


@pytest.mark.parametrize("make", [_whatsapp, _zalo], ids=["whatsapp", "zalo"])
def test_no_answer_is_unconfirmed(make, doc):
    adapter = make(reply=None)
    with pytest.raises(DeliveryUnconfirmed) as caught:
        asyncio.run(adapter.send_file_to_chat_strict("room-1", doc, name="report.pdf"))
    assert isinstance(caught.value, ChannelAuthError)
    assert adapter._pending == {}


@pytest.mark.parametrize("make", [_whatsapp, _zalo], ids=["whatsapp", "zalo"])
def test_a_reported_refusal_is_a_plain_error(make, doc):
    adapter = make(reply={"ok": False, "error": "not a participant"})
    with pytest.raises(ChannelAuthError, match="not a participant") as caught:
        asyncio.run(adapter.send_file_to_chat_strict("room-1", doc, name="report.pdf"))
    assert not isinstance(caught.value, DeliveryUnconfirmed)


@pytest.mark.parametrize("make", [_whatsapp, _zalo], ids=["whatsapp", "zalo"])
def test_an_acknowledged_send_returns(make, doc):
    adapter = make(reply={"ok": True})
    asyncio.run(adapter.send_file_to_chat_strict("room-1", doc, name="report.pdf"))
    assert len(adapter._ws.frames) == 1


def test_whatsapp_addresses_a_room_by_its_jid(doc):
    adapter = _whatsapp(reply={"ok": True})
    asyncio.run(adapter.send_file_to_chat_strict("1203634@g.us", doc, name="report.pdf"))
    frame = adapter._ws.frames[0]
    assert frame["kind"] == "send_file" and frame["sender_id"] == "1203634@g.us"


def test_zalo_says_the_room_is_a_room(doc):
    """Without ``thread_type`` the sidecar assumes a person, and a room's id
    sent as a person's goes to whoever owns that number."""
    adapter = _zalo(reply={"ok": True})
    asyncio.run(adapter.send_file_to_chat_strict("5566", doc, name="report.pdf"))
    assert adapter._ws.frames[0]["thread_type"] == zu_mod._THREAD_GROUP
    asyncio.run(adapter.send_file_strict("7788", doc, name="report.pdf"))
    assert "thread_type" not in adapter._ws.frames[1]


@pytest.mark.parametrize("adapter_cls, room, person", [
    (WhatsappAdapter, "120363041234567@g.us", "84901234567@s.whatsapp.net"),
    (WhatsappAdapter, "status@broadcast", "123456789@lid"),
    (SlackAdapter, "C0123ABCDE", "U0123ABCDE"),
    (SlackAdapter, "G0123ABCDE", "D0123ABCDE"),
    (TelegramAdapter, "-1001234567890", "1644772063"),
    (TelegramUserbotAdapter, "-4567", "4567"),
])
def test_room_shapes_are_told_apart_from_people(adapter_cls, room, person):
    assert adapter_cls.looks_like_room_address(room) is True
    assert adapter_cls.looks_like_room_address(person) is False


def test_a_platform_whose_rooms_look_like_people_says_nothing_by_shape():
    from app.channels.adapters.discord import DiscordAdapter
    from app.channels.adapters.zalo_userbot import ZaloUserbotAdapter as Zalo

    assert DiscordAdapter.looks_like_room_address("1234567890123456789") is False
    assert Zalo.looks_like_room_address("5566778899") is False
