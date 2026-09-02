"""Which path a polled Telegram update takes: the room's, or the sender's DM.

The bug this file exists for: a message written in a group used to fall through
to the private-chat handler, whose reply goes to ``chat_id=int(sender_id)`` — so
a question asked in front of everyone was answered privately to whoever asked
it, and the room saw nothing.

Drives the real ``TelegramAdapter._handle_update`` with hand-built update
objects (the ``python-telegram-bot`` types are plain attribute bags, and the
adapter reads them defensively), so no bot, token, or network is involved.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.channels.adapters.telegram import TelegramAdapter

_CHAT_ID = -1001234


class _Storage:
    """Records channel writes and echoes back the updated row, like the real one."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id, **kwargs):
        self.updates.append((channel_id, dict(kwargs)))
        return {
            "id": channel_id, "profile": "dog", "channel_type": "telegram",
            "mode": "bot", "config": {}, "state": kwargs.get("state") or {},
        }


def _adapter(**state) -> TelegramAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "telegram",
        "mode": "bot", "config": {"bot_token": "t"}, "state": dict(state),
    }
    return TelegramAdapter(channel, _Storage())


def _user(user_id=1644772063, username="alexa", first="Alexa", last="Nguyen",
          is_bot=False):
    return SimpleNamespace(
        id=user_id, username=username, first_name=first, last_name=last,
        is_bot=is_bot,
    )


class _Date:
    """Stands in for the ``datetime`` python-telegram-bot puts on a message."""

    def __init__(self, epoch: float) -> None:
        self._epoch = epoch

    def timestamp(self) -> float:
        return self._epoch


def _group_update(chat_type="supergroup", user=None, text="status?", message_id=42,
                  date=1_700_000_000.0, entities=None, reply_to_message=None):
    return SimpleNamespace(
        update_id=1,
        my_chat_member=None,
        message=SimpleNamespace(
            message_id=message_id,
            text=text,
            date=_Date(date) if date is not None else None,
            from_user=_user() if user is None else user,
            chat=SimpleNamespace(id=_CHAT_ID, type=chat_type, title="Ops room"),
            entities=entities,
            reply_to_message=reply_to_message,
        ),
    )


def _membership_update(old_status="left", new_status="member",
                       chat_type="supergroup", chat_id=_CHAT_ID, title="Ops room"):
    """A ``my_chat_member`` update — how Telegram reports our own status change."""
    return SimpleNamespace(
        update_id=4,
        message=None,
        my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type, title=title),
            old_chat_member=SimpleNamespace(status=old_status),
            new_chat_member=SimpleNamespace(status=new_status),
        ),
    )


def _capture_joins(monkeypatch):
    """Replace the channel-group join handler; the adapter imports it lazily
    inside ``_note_group_joined``, so the module attribute is what to patch."""
    joins: list[dict] = []

    async def _joined(adapter, **kwargs):
        joins.append(kwargs)

    monkeypatch.setattr(
        "app.channels.groups.inbound.handle_group_joined", _joined,
    )
    return joins


def _private_update(text="hello"):
    return SimpleNamespace(
        update_id=2,
        my_chat_member=None,
        message=SimpleNamespace(
            message_id=7,
            text=text,
            from_user=_user(),
            chat=SimpleNamespace(id=1644772063, type="private", title=None),
        ),
    )


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


def test_a_supergroup_message_goes_to_the_room(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_update(_group_update())
        await _settle()

        assert dm_calls == []                       # never the private path
        assert len(group_calls) == 1
        assert group_calls[0] == {
            "chat_id": str(_CHAT_ID),
            "chat_title": "Ops room",
            "chat_type": "supergroup",
            "sender_id": "1644772063",
            "sender_username": "alexa",
            "display_name": "Alexa Nguyen",
            "text": "status?",
            "platform_message_id": "42",
            "platform_message_date": 1_700_000_000.0,
            "sender_is_bot": False,
            # Nobody tagged us in this one; the room decides what that means.
            "mentioned": False,
            "files": None,
        }

    asyncio.run(scenario())


def test_a_mention_reaches_the_room_as_mentioned(monkeypatch):
    """The wiring guard: ``_is_mentioned`` is read per message and forwarded, so
    the room can tell "somebody addressed us" from ordinary chatter."""
    async def scenario():
        adapter = _adapter(self_identity={"user_id": "999", "username": "dogbot",
                                          "is_bot": True})
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._handle_update(_group_update(
            text="@dogbot status?",
            entities=[SimpleNamespace(type="mention", offset=0, length=7)],
        ))
        await _settle()

        assert group_calls[0]["mentioned"] is True

    asyncio.run(scenario())


def test_a_message_with_no_timestamp_still_routes(monkeypatch):
    """The send time only sharpens the dedupe key; losing it must not lose the
    message, and it must never raise inside the poll loop."""
    async def scenario():
        adapter = _adapter()
        group_calls, _ = _capture(monkeypatch, adapter)

        await adapter._handle_update(_group_update(date=None))
        await _settle()

        assert len(group_calls) == 1
        assert group_calls[0]["platform_message_date"] is None

    asyncio.run(scenario())


def test_a_legacy_group_message_also_goes_to_the_room(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_update(_group_update(chat_type="group"))
        await _settle()

        assert dm_calls == []
        assert group_calls[0]["chat_type"] == "group"

    asyncio.run(scenario())


def test_a_bot_author_is_reported_not_hidden(monkeypatch):
    """The group layer decides what to do with it; the adapter must not lie."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._handle_update(
            _group_update(user=_user(user_id=999, username="catbot", is_bot=True)),
        )
        await _settle()

        assert group_calls[0]["sender_is_bot"] is True

    asyncio.run(scenario())


def test_our_own_group_message_is_skipped(monkeypatch):
    async def scenario():
        adapter = _adapter(self_identity={"user_id": "999", "username": "dogbot",
                                          "is_bot": True})
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_update(_group_update(user=_user(user_id=999)))
        await _settle()

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_an_authorless_group_message_is_skipped(monkeypatch):
    """Channel posts and service messages have no ``from_user`` to attribute."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        update = _group_update()
        update.message.from_user = None
        await adapter._handle_update(update)
        await _settle()

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_private_message_still_takes_the_dm_path(monkeypatch):
    """The regression guard: nothing about 1:1 chats changed."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._handle_update(_private_update())
        await _settle()

        assert group_calls == []
        assert dm_calls == [("1644772063", "Alexa Nguyen", "hello")]

    asyncio.run(scenario())


def test_a_textless_update_is_ignored(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        update = _group_update()
        update.message.text = None
        await adapter._handle_update(update)
        await adapter._handle_update(SimpleNamespace(
            update_id=3, my_chat_member=None, message=None,
        ))
        await _settle()

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_being_added_to_a_group_announces_the_join(monkeypatch):
    """Telegram is the only bot transport that reports being added, which is why
    a group here becomes visible before anybody has spoken in it."""
    async def scenario():
        adapter = _adapter()
        joins = _capture_joins(monkeypatch)

        await adapter._handle_update(_membership_update())
        await _settle()

        assert joins == [{
            "chat_id": str(_CHAT_ID),
            "chat_title": "Ops room",
            "chat_type": "supergroup",
        }]

    asyncio.run(scenario())


def test_being_added_to_a_private_chat_is_not_a_join(monkeypatch):
    """A 1:1 chat is the DM pipeline's business — it is not a room to discover."""
    async def scenario():
        adapter = _adapter()
        joins = _capture_joins(monkeypatch)

        await adapter._handle_update(
            _membership_update(chat_type="private", chat_id=42, title=None),
        )
        await _settle()

        assert joins == []

    asyncio.run(scenario())


def test_a_mere_status_change_is_not_a_join(monkeypatch):
    """Telegram sends ``my_chat_member`` for every status change, our own
    promotion to admin included. Treating those as joins would re-discover a
    group the operator has already answered for."""
    async def scenario():
        adapter = _adapter()
        joins = _capture_joins(monkeypatch)

        await adapter._handle_update(
            _membership_update(old_status="member", new_status="administrator"),
        )
        await _settle()

        assert joins == []

    asyncio.run(scenario())


def test_being_removed_from_a_group_is_not_a_join(monkeypatch):
    async def scenario():
        adapter = _adapter()
        joins = _capture_joins(monkeypatch)

        await adapter._handle_update(
            _membership_update(old_status="member", new_status="left"),
        )
        await _settle()

        assert joins == []

    asyncio.run(scenario())


def test_send_to_chat_addresses_the_room(monkeypatch):
    """A room's chat id is negative and belongs to no user — it must not be
    routed through the DM send, which reads it as a sender id."""
    async def scenario():
        adapter = _adapter()
        adapter._bot = object()          # non-None: no bot is built
        sent: list[tuple[int, str]] = []

        async def _send(chat_id, text):
            sent.append((chat_id, text))

        monkeypatch.setattr(adapter, "_send_with_retry", _send)
        await adapter.send_to_chat(str(_CHAT_ID), "mirrored")

        assert sent == [(_CHAT_ID, "mirrored")]
        assert isinstance(sent[0][0], int)

    asyncio.run(scenario())


# --- typing ------------------------------------------------------------------
#
# The indicator runs every four seconds for the whole length of a run, so what
# it does with a failure matters more than it would on a once-per-message call.
#
# ``python-telegram-bot`` is an optional extra and is NOT installed in CI, so
# the two symbols the typing path imports are stood up as modules rather than
# imported — the same rule the rest of this file follows ("no bot, token, or
# network is involved"). The fake error hierarchy mirrors PTB's real one, which
# ``test_bad_request_really_is_a_network_error`` pins wherever the extra exists.


class _NetworkError(Exception):
    pass


class _BadRequest(_NetworkError):
    """PTB really does derive its 400 from its transport error. That is the
    whole hazard the code under test works around."""


class _TypingBot:
    def __init__(self, raises=None):
        self.raises = raises
        self.calls: list[tuple] = []

    async def send_chat_action(self, chat_id, action):
        self.calls.append((chat_id, action))
        if self.raises is not None:
            raise self.raises


@pytest.fixture()
def _ptb(monkeypatch):
    error = ModuleType("telegram.error")
    error.NetworkError = _NetworkError
    error.BadRequest = _BadRequest
    constants = ModuleType("telegram.constants")
    constants.ChatAction = SimpleNamespace(TYPING="typing")
    for name, module in (
        ("telegram", ModuleType("telegram")),
        ("telegram.error", error),
        ("telegram.constants", constants),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    yield


def test_bad_request_really_is_a_network_error():
    """The premise the fakes above encode, checked against the real library
    wherever it is installed. If PTB ever untangled the two, the guard in
    ``_typing_failed`` becomes dead weight rather than load-bearing — and this
    is the test that would say so."""
    pytest.importorskip("telegram", reason="python-telegram-bot is an optional extra")
    from telegram.error import BadRequest, NetworkError  # type: ignore

    assert issubclass(BadRequest, NetworkError)


def test_typing_in_a_room_is_addressed_to_the_room(_ptb):
    async def scenario():
        adapter = _adapter()
        adapter._bot = _TypingBot()
        await adapter._send_typing_to_chat(str(_CHAT_ID))

        assert adapter._bot.calls == [(_CHAT_ID, "typing")]
        assert isinstance(adapter._bot.calls[0][0], int)

    asyncio.run(scenario())


def test_a_transport_failure_rebuilds_the_bot(_ptb, monkeypatch):
    """The stale httpx pool the message-send path also recovers from: without
    the reset the room's indicator would stay dark for the rest of the run."""
    async def scenario():
        adapter = _adapter()
        adapter._bot = _TypingBot(raises=_NetworkError("pool is dead"))
        resets: list[int] = []

        async def _reset():
            resets.append(1)

        monkeypatch.setattr(adapter, "_reset_bot", _reset)
        await adapter._send_typing_to_chat(str(_CHAT_ID))

        assert resets == [1]

    asyncio.run(scenario())


def test_a_rejected_chat_does_not_rebuild_the_bot(_ptb, monkeypatch):
    """``BadRequest`` is a SUBCLASS of ``NetworkError`` in PTB, so catching the
    transport error alone would read a permanent "chat not found" — a room whose
    id outlived our membership — as a stale pool, and tear the httpx client down
    and build it back up every four seconds for as long as that room is
    answered, silently."""
    async def scenario():
        adapter = _adapter()
        adapter._bot = _TypingBot(raises=_BadRequest("chat not found"))
        resets: list[int] = []

        async def _reset():
            resets.append(1)

        monkeypatch.setattr(adapter, "_reset_bot", _reset)
        await adapter._send_typing_to_chat(str(_CHAT_ID))

        assert resets == []

    asyncio.run(scenario())


def test_a_rejected_sender_does_not_rebuild_the_bot_either(_ptb, monkeypatch):
    """The DM path shares the decision, so it cannot drift from the room's."""
    async def scenario():
        adapter = _adapter()
        adapter._bot = _TypingBot(raises=_BadRequest("chat not found"))
        resets: list[int] = []

        async def _reset():
            resets.append(1)

        monkeypatch.setattr(adapter, "_reset_bot", _reset)
        await adapter._send_typing("1644772063")

        assert resets == []

    asyncio.run(scenario())


def test_store_self_identity_persists_it_on_the_channel_row():
    """Who "we" are has to survive a restart: a group recognises its own posts
    from the persisted identity, before any adapter has connected."""
    async def scenario():
        adapter = _adapter()
        await adapter._store_self_identity(
            user_id="777", username="@dogbot", is_bot=True,
        )

        stored = adapter.channel["state"]["self_identity"]
        assert stored == {"user_id": "777", "username": "dogbot", "is_bot": True}
        assert adapter.storage.updates[0][0] == "c1"

    asyncio.run(scenario())


# --- _is_mentioned -----------------------------------------------------------
#
# Searching the raw text for "@name" is not good enough: it would match somebody
# spelling the bot's name inside a sentence, and would miss a ``text_mention``,
# which is how a user without a username is tagged. So the entities are what is
# read, and these pin down each of the three ways Telegram says "this is for you".

def _mentionable_adapter() -> TelegramAdapter:
    return _adapter(self_identity={"user_id": "999", "username": "dogbot",
                                   "is_bot": True})


def test_a_mention_entity_naming_our_username_counts_as_addressed():
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(
        text="@dogbot status?",
        entities=[SimpleNamespace(type="mention", offset=0, length=7)],
        reply_to_message=None,
    )

    assert adapter._is_mentioned(msg) is True


def test_a_text_mention_entity_carrying_our_user_id_counts_as_addressed():
    """How somebody tags an account that has no username — there is no ``@name``
    in the text at all, only an entity pointing at the user id."""
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(
        text="Dogbot status?",
        entities=[SimpleNamespace(type="text_mention", offset=0, length=6,
                                  user=SimpleNamespace(id=999))],
        reply_to_message=None,
    )

    assert adapter._is_mentioned(msg) is True


def test_a_reply_to_one_of_our_own_messages_counts_as_addressed():
    """In a Telegram group this is how people keep talking to the bot without
    re-typing its name, so a reply has to read as a mention."""
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(
        text="and now?",
        entities=None,
        reply_to_message=SimpleNamespace(from_user=_user(user_id=999)),
    )

    assert adapter._is_mentioned(msg) is True


def test_a_reply_to_somebody_else_is_not_addressed_to_us():
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(
        text="and now?",
        entities=None,
        reply_to_message=SimpleNamespace(from_user=_user(user_id=1644772063)),
    )

    assert adapter._is_mentioned(msg) is False


def test_a_mention_of_somebody_else_is_not_addressed_to_us():
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(
        text="@catbot status?",
        entities=[SimpleNamespace(type="mention", offset=0, length=7)],
        reply_to_message=None,
    )

    assert adapter._is_mentioned(msg) is False


def test_a_plain_message_with_no_entities_is_not_addressed_to_us():
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(text="status?", entities=None, reply_to_message=None)

    assert adapter._is_mentioned(msg) is False


def test_a_message_shaped_unexpectedly_reads_as_not_mentioned():
    """This runs on every group message: a surprise must never raise inside the
    poll loop, it must answer "no"."""
    adapter = _mentionable_adapter()
    msg = SimpleNamespace(
        text="@dogbot status?",
        # An entity with no offset/length at all — nothing to slice.
        entities=[SimpleNamespace(type="mention")],
        reply_to_message=None,
    )

    assert adapter._is_mentioned(msg) is False
