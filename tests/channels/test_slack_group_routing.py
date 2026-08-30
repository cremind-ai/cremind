"""Which path a Slack ``message`` event takes: the room's, or the sender's DM.

The bug this file exists for: the handler returned immediately unless the event's
``channel_type`` was ``im``, and returned again on ANY subtype. A message posted
in a channel therefore reached nothing at all, and even once channel events were
subscribed to, the blanket subtype check would have thrown away every
``bot_message`` — which is how the other members' mirrors arrive — before the
group layer could decide whose echo it was.

Drives the real ``SlackAdapter`` with hand-built event dicts (Slack delivers
plain JSON, and ``slack-bolt`` is an optional extra that is not installed here),
so no app, token, or socket is involved.
"""

from __future__ import annotations

import asyncio

from app.channels.adapters.slack import SlackAdapter

_CHAT_ID = "C0OPSROOM"
_TS = "1700000000.000300"


class _Storage:
    """Records channel writes and echoes back the updated row, like the real one."""

    def __init__(self):
        self.updates: list[tuple[str, dict]] = []

    async def update_channel(self, channel_id, **kwargs):
        self.updates.append((channel_id, dict(kwargs)))
        return {
            "id": channel_id, "profile": "dog", "channel_type": "slack",
            "mode": "bot", "config": {}, "state": kwargs.get("state") or {},
        }


class _FakeClient:
    def __init__(self):
        self.users_info_calls: list[str] = []
        self.channel_info_calls: list[str] = []
        self.opened: list[str] = []
        self.posted: list[tuple[str, str]] = []

    async def users_info(self, user):
        self.users_info_calls.append(user)
        return {
            "user": {
                "name": "alexa",
                "real_name": "Alexa Nguyen",
                "profile": {"display_name": "Alexa"},
            },
        }

    async def conversations_info(self, channel):
        self.channel_info_calls.append(channel)
        return {"channel": {"id": channel, "name": "ops-room"}}

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": "D-opened"}}

    async def chat_postMessage(self, channel, text):
        self.posted.append((channel, text))
        return {"ok": True}

    async def auth_test(self):
        return {"user_id": "U0BOT", "user": "cremind"}


class _FakeApp:
    def __init__(self):
        self.client = _FakeClient()


class _FakeHandler:
    def __init__(self):
        self.connected = False

    async def connect_async(self):
        self.connected = True


def _adapter(**state) -> SlackAdapter:
    channel = {
        "id": "c1", "profile": "dog", "channel_type": "slack",
        "mode": "bot", "config": {"bot_token": "xoxb-1", "app_token": "xapp-1"},
        "state": dict(state),
    }
    adapter = SlackAdapter(channel, _Storage())
    adapter._app = _FakeApp()
    return adapter


def _event(channel_type="channel", text="status?", user="U1ALEXA", ts=_TS,
           channel=_CHAT_ID, subtype=None, bot_id=None) -> dict:
    event: dict = {
        "channel_type": channel_type, "channel": channel, "text": text, "ts": ts,
    }
    if user is not None:
        event["user"] = user
    if subtype is not None:
        event["subtype"] = subtype
    if bot_id is not None:
        event["bot_id"] = bot_id
    return event


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


def test_a_channel_message_goes_to_the_room(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event())

        assert dm_calls == []                       # never the private path
        assert len(group_calls) == 1
        assert group_calls[0] == {
            "chat_id": _CHAT_ID,
            "chat_title": "ops-room",
            "chat_type": "channel",
            "sender_id": "U1ALEXA",
            "sender_username": "alexa",
            "display_name": "Alexa",
            "text": "status?",
            "platform_message_id": _TS,
            "platform_message_date": 1_700_000_000.0003,
            "sender_is_bot": False,
            # Nobody tagged us in this one; the room decides what that means.
            "mentioned": False,
        }

    asyncio.run(scenario())


def test_a_private_channel_message_goes_to_the_room(monkeypatch):
    """Slack calls a private channel a "group", which is NOT Telegram's "group":
    its message ids are per chat, so they must stay the dedupe key."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event(channel_type="group"))

        assert dm_calls == []
        assert group_calls[0]["chat_type"] == "group"
        assert group_calls[0]["platform_message_id"] == _TS

    asyncio.run(scenario())


def test_a_group_dm_goes_to_the_room(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event(channel_type="mpim"))

        assert dm_calls == []
        assert group_calls[0]["chat_type"] == "mpim"

    asyncio.run(scenario())


def test_the_timestamp_is_both_the_id_and_the_send_time(monkeypatch):
    """Slack's ``ts`` is a string of epoch seconds, so it cannot travel through
    ``platform_message_timestamp`` (which reads a ``.date`` attribute)."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event(ts="1700000123.456789"))

        assert group_calls[0]["platform_message_id"] == "1700000123.456789"
        assert group_calls[0]["platform_message_date"] == 1_700_000_123.456789

    asyncio.run(scenario())


def test_a_bot_post_reaches_the_group_layer(monkeypatch):
    """Every mirror the other members post arrives as ``bot_message``; dropping
    it here would hide our own room's echo from the only layer that can spot it."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(
            _event(subtype="bot_message", user=None, bot_id="B0CAT",
                   text="Cat: on it"),
        )

        assert len(group_calls) == 1
        assert group_calls[0]["sender_is_bot"] is True
        # A bot post carries no ``user``, so its ``bot_id`` is its whole identity.
        assert group_calls[0]["sender_id"] == "B0CAT"
        assert group_calls[0]["sender_username"] is None
        # And no ``users.info`` was spent on an id that has no user behind it.
        assert adapter._app.client.users_info_calls == []

    asyncio.run(scenario())


def test_a_user_post_carrying_a_bot_id_is_still_flagged(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event(bot_id="B0CAT"))

        assert group_calls[0]["sender_is_bot"] is True
        assert group_calls[0]["sender_id"] == "U1ALEXA"

    asyncio.run(scenario())


def test_a_thread_broadcast_still_reaches_the_room(monkeypatch):
    """A threaded reply "also sent to the channel" is an ordinary post to
    everyone reading the room."""
    async def scenario():
        adapter = _adapter()
        group_calls, _dm = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event(subtype="thread_broadcast"))

        assert len(group_calls) == 1

    asyncio.run(scenario())


def test_an_edit_or_a_join_is_dropped(monkeypatch):
    """The timeline has no row to revise and nothing to say about a join."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        for subtype in ("message_changed", "message_deleted", "channel_join",
                        "file_share"):
            await adapter._dispatch_message_event(_event(subtype=subtype))

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_textless_channel_message_is_ignored(monkeypatch):
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event(text=""))
        await adapter._dispatch_message_event(_event(user=None))

        assert group_calls == []
        assert dm_calls == []

    asyncio.run(scenario())


def test_a_dm_still_takes_the_dm_path(monkeypatch):
    """The regression guard: nothing about 1:1 chats changed."""
    async def scenario():
        adapter = _adapter()
        group_calls, dm_calls = _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(
            _event(channel_type="im", channel="D0ALEXA", text="hello"),
        )

        assert group_calls == []
        assert dm_calls == [("U1ALEXA", "Alexa", "hello")]
        # The IM channel is still cached off the event for the reply.
        assert adapter._im_channels["U1ALEXA"] == "D0ALEXA"

    asyncio.run(scenario())


def test_names_are_looked_up_once_per_id(monkeypatch):
    """Every message in a busy channel would otherwise cost two API calls."""
    async def scenario():
        adapter = _adapter()
        _capture(monkeypatch, adapter)

        await adapter._dispatch_message_event(_event())
        await adapter._dispatch_message_event(_event(text="any news?"))

        client = adapter._app.client
        assert client.users_info_calls == ["U1ALEXA"]
        assert client.channel_info_calls == [_CHAT_ID]

    asyncio.run(scenario())


def test_send_to_chat_addresses_the_room():
    """A room is nobody's sender, so the channel id must not be run through the
    DM resolver — whose cache is keyed by user and whose fallback opens a DM."""
    async def scenario():
        adapter = _adapter()
        await adapter.send_to_chat(_CHAT_ID, "mirrored")

        client = adapter._app.client
        assert client.posted == [(_CHAT_ID, "mirrored")]
        assert client.opened == []

    asyncio.run(scenario())


def test_connecting_records_the_mention_that_pings_us(monkeypatch):
    """``@handle`` is plain text in Slack — only ``<@U…>`` notifies the app, and
    the roster hands the other agents whatever this records.

    The display name is recorded alongside it because that is what a channel
    shows above the app's messages, and so what somebody types when they mean
    to address it in words rather than with a mention.
    """
    async def scenario():
        adapter = _adapter()
        handler = _FakeHandler()
        monkeypatch.setattr(adapter, "_build", lambda: (adapter._app, handler))

        run = asyncio.create_task(adapter._run())
        await asyncio.sleep(0)          # _run parks on an Event once connected
        run.cancel()
        try:
            await run
        except asyncio.CancelledError:
            pass

        assert handler.connected is True
        assert adapter.channel["state"]["self_identity"] == {
            "user_id": "U0BOT", "username": "cremind", "is_bot": True,
            "mention": "<@U0BOT>", "display_name": "Alexa",
        }

    asyncio.run(scenario())

# --- _is_mentioned -----------------------------------------------------------
#
# "@handle" is plain text in Slack: only the "<@U…>" token is a real ping, so
# that token — and a threaded reply to something the app itself said — is the
# whole of what counts as being addressed.

def _mentionable_adapter() -> SlackAdapter:
    return _adapter(self_identity={
        "user_id": "U0BOT", "username": "cremind", "is_bot": True,
        "mention": "<@U0BOT>",
    })


def test_the_mention_token_counts_as_addressed():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({}, "<@U0BOT> status?") is True


def test_a_reply_in_a_thread_we_started_counts_as_addressed():
    """``parent_user_id`` names whoever wrote the message the thread hangs off;
    answering there is how a Slack conversation continues without re-tagging."""
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"parent_user_id": "U0BOT"}, "and now?") is True


def test_a_thread_somebody_else_started_is_not_addressed_to_us():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({"parent_user_id": "U1ALEXA"}, "and now?") is False


def test_naming_our_handle_in_plain_text_is_not_a_mention():
    """The one that would be a false positive: "@cremind" pings nobody in Slack
    and is just somebody talking about the app."""
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({}, "ask @cremind about it") is False


def test_a_mention_of_somebody_else_is_not_addressed_to_us():
    adapter = _mentionable_adapter()

    assert adapter._is_mentioned({}, "<@U1ALEXA> status?") is False


def test_before_auth_test_has_answered_nothing_reads_as_addressed():
    """Without our own id there is nothing to compare against, and guessing the
    wrong way would wake the agent on every message in the channel."""
    adapter = _adapter()

    assert adapter._is_mentioned({"parent_user_id": "U0BOT"}, "<@U0BOT> hi") is False
