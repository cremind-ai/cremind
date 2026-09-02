"""What the Zalo personal account does with the rooms it is a member of.

The sidecar used to drop every group thread on the floor ("DM-only in v1"), so a
personal Zalo account could not carry a bound room at all. It now forwards one as
an ``incoming_group`` frame, and this file pins what the adapter does with it: a
room's message goes to the group timeline, a DM still goes to that person's
conversation, and an outbound room message is marked as a GROUP thread — without
that marker the sidecar sends to whoever owns the same id as a user.

Drives the real ``ZaloUserbotAdapter._handle_sidecar_event`` with hand-built
frames, so no Node sidecar, session, or WebSocket is involved.
"""

from __future__ import annotations

import asyncio
import json

from app.channels.adapters.zalo_userbot import ZaloUserbotAdapter

_CHAT_ID = "9876543210"
_SENDER_ID = "1644772063"
_SELF_ID = "5550001111"


class _Storage:
    """Records channel writes and echoes back the updated row, like the real one."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id, **kwargs):
        self.updates.append((channel_id, dict(kwargs)))
        return {
            "id": channel_id, "profile": "dog", "channel_type": "zalo",
            "mode": "userbot", "config": {}, "state": kwargs.get("state") or {},
        }


class _Socket:
    """The sidecar WebSocket, reduced to the frames the adapter writes into it."""

    def __init__(self):
        self.frames: list[dict] = []

    async def send(self, raw):
        self.frames.append(json.loads(raw))


def _adapter(**state) -> ZaloUserbotAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "zalo",
        "mode": "userbot", "config": {}, "state": dict(state),
    }
    return ZaloUserbotAdapter(channel, _Storage())


def _group_frame(**overrides):
    frame = {
        "kind": "incoming_group",
        "chat_id": _CHAT_ID,
        "chat_title": "Ops room",
        "sender_id": _SENDER_ID,
        "display_name": "Alexa Nguyen",
        "message_id": "msg-42",
        "timestamp": 1_700_000_000.0,
        "text": "status?",
    }
    frame.update(overrides)
    return frame


async def _settle():
    # Inbound frames are dispatched as tasks so the WS reader loop keeps reading.
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


def test_a_dm_frame_still_takes_the_conversation_path(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event({
            "kind": "incoming", "sender_id": _SENDER_ID,
            "display_name": "Alexa Nguyen", "text": "hello",
        })
        await _settle()

        assert group_calls == []
        assert dm_calls == [(_SENDER_ID, "Alexa Nguyen", "hello")]

    asyncio.run(scenario())


def test_a_room_frame_goes_to_the_group(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_group_frame())
        await _settle()

        assert dm_calls == []
        assert group_calls == [{
            "chat_id": _CHAT_ID,
            "chat_title": "Ops room",
            "chat_type": "zalo_group",
            "sender_id": _SENDER_ID,
            "sender_username": None,
            "display_name": "Alexa Nguyen",
            "text": "status?",
            "platform_message_id": "msg-42",
            "platform_message_date": 1_700_000_000.0,
            "sender_is_bot": False,
            # Nobody pinged us in this one; the room decides what that means.
            "mentioned": False,
            "files": None,
        }]

    asyncio.run(scenario())


def test_a_nameless_room_still_routes(monkeypatch):
    """``getGroupInfo`` is best-effort in the sidecar; a room we cannot name is
    still a room, and the id is what binds it."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(
            _group_frame(chat_title=None, message_id=None, timestamp=None),
        )
        await _settle()

        assert group_calls[0]["chat_title"] is None
        assert group_calls[0]["platform_message_id"] is None
        assert group_calls[0]["platform_message_date"] is None

    asyncio.run(scenario())


def test_a_room_frame_missing_its_room_or_author_is_dropped(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_sidecar_event(_group_frame(chat_id=""))
        await adapter._handle_sidecar_event(_group_frame(sender_id=""))
        await adapter._handle_sidecar_event(_group_frame(text=""))
        await _settle()

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_photo_that_could_not_be_downloaded_still_reaches_the_room(monkeypatch):
    """The empty-frame guard above is exactly what used to swallow a photo.

    A caption-less photo whose CDN download fails has no text and no file, so
    it looks like the empty frame the previous test drops. The sidecar
    therefore synthesises a notice into ``text`` (see media.js
    ``mediaFailureNotice``); this pins that such a frame still routes, so the
    agent can tell the sender their photo never arrived.
    """
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)
        notice = "[sent a photo, but it could not be downloaded (HTTP 404)]"

        await adapter._handle_sidecar_event(_group_frame(text=notice, files=[]))
        await _settle()

        assert dm_calls == []
        assert group_calls[0]["text"] == notice
        assert group_calls[0]["files"] is None

    asyncio.run(scenario())


def test_ready_records_the_account_the_room_must_ignore():
    """This account receives the mirrors the member agents post into the room;
    without its own id recorded, its answers come back in as new questions."""
    async def scenario():
        adapter = _adapter()
        await adapter._handle_sidecar_event({"kind": "ready", "self_id": _SELF_ID})

        assert adapter.channel["state"]["self_identity"] == {
            "user_id": _SELF_ID, "username": None, "is_bot": False,
        }
        assert adapter.storage.updates[0][0] == "c1"

    asyncio.run(scenario())


def test_a_ready_without_an_id_is_not_fatal():
    """An older sidecar reports no ``self_id``; pairing must still complete."""
    async def scenario():
        adapter = _adapter()
        await adapter._handle_sidecar_event({"kind": "ready"})

        assert adapter.channel["state"] == {}
        assert adapter.storage.updates == []

    asyncio.run(scenario())


def test_send_to_chat_marks_the_frame_as_a_group_thread():
    """The sidecar assumes a user thread when the frame does not say otherwise,
    which would deliver the room's answer to whoever owns that id."""
    async def scenario():
        adapter = _adapter()
        adapter._ws = _Socket()
        await adapter.send_to_chat(_CHAT_ID, "mirrored")

        assert adapter._ws.frames == [{
            "kind": "send", "sender_id": _CHAT_ID, "text": "mirrored",
            "thread_type": 1,
        }]

    asyncio.run(scenario())


def test_a_long_message_is_split_rather_than_cut():
    """The sidecar slices a ``send`` frame at 2000 characters instead of
    chunking it, so anything past that used to vanish without a trace."""
    async def scenario():
        adapter = _adapter()
        adapter._ws = _Socket()
        await adapter.send_to_chat(_CHAT_ID, "x" * 4500)
        await adapter._send_text(_SENDER_ID, "y" * 4500)

        texts = [frame["text"] for frame in adapter._ws.frames]
        assert len(texts) >= 6
        assert all(len(text) <= 2000 for text in texts)
        assert sum(len(text) for text in texts) == 9000

    asyncio.run(scenario())


def test_the_room_is_never_sent_markup():
    """Zalo renders text literally, so the mirror's emphasis has to disappear
    rather than arrive as stray asterisks around somebody's name."""
    adapter = _adapter()

    assert adapter.bold("Ana") == "Ana"
    assert adapter.italic("Thought") == "Thought"


# --- _is_mentioned -----------------------------------------------------------
#
# On Zalo a mention is a structured annotation, never text, so the sidecar hands
# over the uids it found and the adapter only has to recognise its own. Replying
# to a message counts too: that is how a Zalo conversation continues.

def _mentionable_adapter() -> ZaloUserbotAdapter:
    return _adapter(self_identity={
        "user_id": _SELF_ID, "username": None, "is_bot": False,
    })


def test_a_ping_naming_our_uid_counts_as_addressed():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"mentioned_ids": [_SENDER_ID, _SELF_ID]}) is True


def test_a_reply_to_one_of_our_own_messages_counts_as_addressed():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"quoted_sender_id": _SELF_ID}) is True


def test_a_ping_naming_somebody_else_is_not_addressed_to_us():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned(
        {"mentioned_ids": [_SENDER_ID], "quoted_sender_id": _SENDER_ID},
    ) is False


def test_a_frame_with_no_annotations_is_not_addressed_to_us():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({}) is False


def test_before_ready_nothing_reads_as_addressed():
    """No ``self_id`` recorded yet — there is nothing to match, and claiming a
    mention would wake the agent on every message in the room."""
    adapter = _adapter()

    assert adapter._is_mentioned({"mentioned_ids": [_SELF_ID]}) is False
