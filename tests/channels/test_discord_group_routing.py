"""Which path a Discord gateway message takes: the room's, the sender's DM, none.

Server-channel messages used to be dropped outright ("this is a 1:1 DM bridge"),
so a Discord channel could not carry a Cremind room at all. Undoing that is not
just deleting the line: the bot filter ran first, and Discord — unlike Telegram —
really does deliver one bot's messages to another. A sibling member bot's mirror
of the timeline has to arrive at the group layer flagged as bot-authored so the
drop happens THERE, where the channel is also recorded as one an operator could
bind. Dropped in the adapter instead, a room whose traffic is all bots looks to
Cremind like a room nobody has ever written in.

Drives the real ``DiscordAdapter._dispatch_message`` with hand-built message
objects (``discord.py`` is an optional extra, not installed here, and the adapter
reads a message defensively), so no gateway, token or network is involved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.channels.adapters.discord import DiscordAdapter, _is_mentioned

_CHANNEL_ID = 1122334455
_OWN_ID = 777


class _Storage:
    """Records channel writes and echoes back the updated row, like the real one."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id, **kwargs):
        self.updates.append((channel_id, dict(kwargs)))
        return {
            "id": channel_id, "profile": "dog", "channel_type": "discord",
            "mode": "bot", "config": {}, "state": kwargs.get("state") or {},
        }


def _adapter(**state) -> DiscordAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "discord",
        "mode": "bot", "config": {"bot_token": "t"}, "state": dict(state),
    }
    return DiscordAdapter(channel, _Storage())


def _client(user_id=_OWN_ID):
    """The gateway client, which is only read for "who am I"."""
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id) if user_id is not None else None,
    )


class _Created:
    """Stands in for the ``datetime`` discord.py puts on ``created_at``."""

    def __init__(self, epoch: float) -> None:
        self._epoch = epoch

    def timestamp(self) -> float:
        return self._epoch


def _author(user_id=42, name="alexa", display="Alexa Nguyen", bot=False):
    return SimpleNamespace(id=user_id, name=name, display_name=display, bot=bot)


def _guild_message(author=None, text="status?", message_id=987,
                   created=1_700_000_000.0, channel_name="general"):
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=_Created(created) if created is not None else None,
        author=_author() if author is None else author,
        guild=SimpleNamespace(name="Ops"),
        channel=SimpleNamespace(id=_CHANNEL_ID, name=channel_name),
    )


def _dm_message(author=None, text="hello"):
    return SimpleNamespace(
        id=988,
        content=text,
        created_at=None,
        author=_author() if author is None else author,
        guild=None,
        channel=SimpleNamespace(id=42, name=None),
    )


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


def test_a_server_channel_message_goes_to_the_room(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(_client(), _guild_message())

        assert dm_calls == []                       # never the private path
        assert len(group_calls) == 1
        assert group_calls[0] == {
            "chat_id": "1122334455",
            "chat_title": "Ops#general",
            "chat_type": "guild_text",
            "sender_id": "42",
            "sender_username": "alexa",
            "display_name": "Alexa Nguyen",
            "text": "status?",
            "platform_message_id": "987",
            "platform_message_date": 1_700_000_000.0,
            "sender_is_bot": False,
            # Nobody tagged us in this one; the room decides what that means.
            "mentioned": False,
            "files": None,
        }

    asyncio.run(scenario())


def test_a_mention_token_reaches_the_room_unrewritten(monkeypatch):
    """``<@id>`` is how an agent knows it was the one addressed; ``clean_content``
    would hand the room "@dogbot", which pings nobody and names nobody."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(
            _client(), _guild_message(text="<@777> status?"),
        )

        assert group_calls[0]["text"] == "<@777> status?"
        # And the room is told, so it need not re-derive it from the token.
        assert group_calls[0]["mentioned"] is True

    asyncio.run(scenario())


def test_a_bot_author_is_reported_not_hidden(monkeypatch):
    """The ordering guard. Discord delivers one bot's messages to another, so a
    sibling's mirror must reach the group layer to be dropped by it."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(
            _client(),
            _guild_message(author=_author(user_id=999, name="catbot", bot=True)),
        )

        assert dm_calls == []
        assert len(group_calls) == 1
        assert group_calls[0]["sender_is_bot"] is True

    asyncio.run(scenario())


def test_a_message_with_no_timestamp_still_routes(monkeypatch):
    """The send time only sharpens the dedupe key; losing it must not lose the
    message, and it must never raise inside the gateway loop."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(_client(), _guild_message(created=None))

        assert len(group_calls) == 1
        assert group_calls[0]["platform_message_date"] is None

    asyncio.run(scenario())


def test_a_nameless_channel_still_carries_a_title(monkeypatch):
    """A title is what the operator picks the room by when binding, so half of
    one beats none."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(
            _client(), _guild_message(channel_name=None),
        )

        assert group_calls[0]["chat_title"] == "Ops"

    asyncio.run(scenario())


def test_our_own_server_message_is_skipped(monkeypatch):
    """Our mirror of the timeline comes straight back down the gateway."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(
            _client(), _guild_message(author=_author(user_id=_OWN_ID)),
        )

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_an_authorless_message_is_skipped(monkeypatch):
    """System messages (pins, joins) have nobody to attribute the message to."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        message = _guild_message()
        message.author = None
        await adapter._dispatch_message(_client(), message)

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_direct_message_still_takes_the_dm_path(monkeypatch):
    """The regression guard: nothing about 1:1 chats changed."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(_client(), _dm_message())

        assert group_calls == []
        assert dm_calls == [("42", "Alexa Nguyen", "hello")]

    asyncio.run(scenario())


def test_a_bot_dm_is_still_ignored(monkeypatch):
    """Only the room path forwards bots. A bot DMing this one has no room to
    teach anything about, and answering it is how two bots loop forever."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(
            _client(), _dm_message(author=_author(user_id=999, bot=True)),
        )

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_an_empty_direct_message_is_ignored(monkeypatch):
    """Attachment-only messages arrive with no content once the intent strips it."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message(_client(), _dm_message(text=""))

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_send_to_chat_addresses_the_channel_within_discords_own_limit():
    """The base chunker cuts at 3500, which Discord rejects outright at 2000 —
    a mirrored bubble between the two would not arrive shortened, it would not
    arrive at all."""
    sent: list[str] = []

    class _Channel:
        async def send(self, text):
            sent.append(text)

    async def scenario():
        adapter = _adapter()
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: _Channel() if cid == _CHANNEL_ID else None,
        )
        await adapter.send_to_chat(str(_CHANNEL_ID), "y" * 5000)

    asyncio.run(scenario())
    assert len(sent) >= 3
    assert all(len(chunk) <= 2000 for chunk in sent)


def test_a_channel_the_session_has_not_seen_is_fetched():
    """``get_channel`` reads the gateway's cache, which is empty right after a
    restart — exactly when the mirror has a backlog to post."""
    fetched: list[int] = []

    class _Channel:
        async def send(self, text):
            pass

    async def _fetch(channel_id):
        fetched.append(channel_id)
        return _Channel()

    async def scenario():
        adapter = _adapter()
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: None, fetch_channel=_fetch,
        )
        await adapter.send_to_chat(str(_CHANNEL_ID), "mirrored")

    asyncio.run(scenario())
    assert fetched == [_CHANNEL_ID]


def test_typing_in_a_room_survives_a_cold_gateway_cache():
    """The indicator goes up *before* the answer, so it is the one call most
    likely to run on a cold cache — a cache-only lookup here is how the reply
    used to arrive with no "typing…" ahead of it."""
    fetched: list[int] = []
    typed: list[str] = []

    class _Channel:
        async def typing(self):
            typed.append("pulse")

    async def _fetch(channel_id):
        fetched.append(channel_id)
        return _Channel()

    async def scenario():
        adapter = _adapter()
        adapter._client = SimpleNamespace(
            get_channel=lambda cid: None, fetch_channel=_fetch,
        )
        await adapter._send_typing_to_chat(str(_CHANNEL_ID))

    asyncio.run(scenario())
    assert fetched == [_CHANNEL_ID]
    assert typed == ["pulse"]


def test_bold_is_two_asterisks_because_one_is_italic_here():
    """One room's text is mirrored to every platform bound to it, so the mirror
    asks the carrying adapter for emphasis instead of writing Telegram's."""
    assert _adapter().bold("Rex") == "**Rex**"
    assert _adapter().italic("thinking") == "_thinking_"


def test_the_stored_identity_is_addressable_on_discord():
    """What ``on_ready`` records. Discord pings by id — "@dogbot" in a message
    body is ordinary text — so without the mention the roster hands the other
    agents a handle that reaches nobody."""
    async def scenario():
        adapter = _adapter()
        await adapter._store_self_identity(
            user_id=str(_OWN_ID), username="dogbot", is_bot=True,
            mention=f"<@{_OWN_ID}>",
        )

        stored = adapter.channel["state"]["self_identity"]
        assert stored == {
            "user_id": "777", "username": "dogbot", "is_bot": True,
            "mention": "<@777>",
        }
        assert adapter.storage.updates[0][0] == "c1"

    asyncio.run(scenario())


# --- _is_mentioned -----------------------------------------------------------
#
# Discord says "this one is for you" in three different shapes, and the room
# needs all three: the resolved ``mentions`` list, the raw ``<@id>`` token the
# dispatcher deliberately keeps unrewritten, and a reply — which pings by default
# and is how people keep talking to a bot without re-typing its name.

def test_a_resolved_mention_of_our_bot_counts_as_addressed():
    message = _guild_message()
    message.mentions = [SimpleNamespace(id=_OWN_ID)]

    assert _is_mentioned(_client(), message) is True


def test_the_raw_mention_token_counts_as_addressed():
    message = _guild_message(text=f"<@{_OWN_ID}> status?")

    assert _is_mentioned(_client(), message) is True


def test_the_nickname_form_of_the_token_counts_too():
    """``<@!id>`` is the same ping written by an older client."""
    message = _guild_message(text=f"<@!{_OWN_ID}> status?")

    assert _is_mentioned(_client(), message) is True


def test_a_reply_to_one_of_our_own_messages_counts_as_addressed():
    message = _guild_message(text="and now?")
    message.reference = SimpleNamespace(
        resolved=SimpleNamespace(author=SimpleNamespace(id=_OWN_ID)),
    )

    assert _is_mentioned(_client(), message) is True


def test_a_mention_of_somebody_else_is_not_addressed_to_us():
    message = _guild_message(text="<@999> status?")
    message.mentions = [SimpleNamespace(id=999)]

    assert _is_mentioned(_client(), message) is False


def test_a_plain_room_message_is_not_addressed_to_us():
    assert _is_mentioned(_client(), _guild_message()) is False


def test_a_gateway_that_has_not_said_who_we_are_reads_as_not_addressed():
    """Before ``on_ready`` there is no id to compare against, and guessing the
    wrong way would wake every agent on every message in the room."""
    message = _guild_message(text=f"<@{_OWN_ID}> status?")

    assert _is_mentioned(_client(user_id=None), message) is False
