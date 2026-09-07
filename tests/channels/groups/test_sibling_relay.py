"""Two of your own Telegram bots in one group, finally hearing each other.

Telegram never delivers a bot's message to another bot. It is a platform rule
rather than a privacy-mode setting, so there is nothing an operator can switch
on to change it — and that is why this file exists. cremind_bot (profile
``admin``) and javis_1511_bot (profile ``bobo``) were put in one group and asked
to introduce themselves and talk. Each answered the human exactly once and the
room went quiet, because neither was ever told the other had replied and so
neither had anything to reply to. From inside each agent the conversation simply
stopped.

:mod:`app.channels.groups.relay` closes that gap in-process, and both halves of
it are pinned here. The post really does arrive at the sibling as an ordinary
bot-authored inbound carrying the sending account's identity, through the whole
ordinary pipeline — and it arrives ONLY where it should: never back to the
channel that spoke, never to a transport the platform already delivered it to,
and never to a channel that has no approved row for the room. That last negative
is the sharp one: relaying to a channel the platform never told about this chat
would manufacture a pending group and an operator notification out of nothing.

``RoomAdapter`` declares ``receives_bot_posts = False`` (see ``conftest``), so
every adapter here stands in for the Telegram bot that needs the relay.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels.base import BaseChannelAdapter
from app.channels.groups.relay import relay_agent_post, relay_candidates
from app.channels.reply_target import group_target
from tests.channels.groups.conftest import RoomAdapter, make_adapter

# What Cremind says first, in the reported scenario. It names Javis with the
# handle Telegram would have shown, so the receiving pipeline's own
# ``_text_mentions_self`` reads it as an address — which is the point: the relay
# claims no mention of its own and lets the sibling decide.
_HELLO = "@javis_1511_bot — I'm Cremind, the other agent in here."


# ── the process the relay looks around in ─────────────────────────────────


class _Registry:
    """The one question :mod:`app.channels.groups.relay` asks a registry.

    Stubbed rather than built: a real ``ChannelRegistry`` wants storage, a
    channel table and a start/stop lifecycle to answer "who else of ours is on
    this platform right now", and that sentence is the whole of what the relay
    needs from it.
    """

    def __init__(self, *adapters):
        self._adapters = list(adapters)
        # Every platform it was asked about, so a test can pin that the relay
        # scoped the question rather than the stub having quietly scoped it for
        # the relay.
        self.asked: list[str] = []

    def adapters_for_channel_type(self, channel_type):
        self.asked.append(channel_type)
        return [a for a in self._adapters if a.channel_type == channel_type]


def _registry(monkeypatch, *adapters) -> _Registry:
    """Make ``adapters`` the live channels of this process.

    Patched on the registry MODULE rather than on the relay, because the relay
    imports ``get_channel_registry`` inside the function that uses it: there is
    no name bound in the relay's own namespace to patch, and the one it resolves
    is the module attribute as it stands at call time.
    """
    import app.channels.registry as registry_mod

    registry = _Registry(*adapters)
    monkeypatch.setattr(
        registry_mod, "get_channel_registry", lambda *a, **k: registry,
    )
    return registry


def _no_registry(monkeypatch) -> None:
    """No channel subsystem at all: a CLI process, or the server still booting.

    Pinned explicitly instead of relying on the real singleton happening to be
    unset, because whether some earlier test in the run initialised it is not
    something this file should be able to depend on.
    """
    import app.channels.registry as registry_mod

    def _uninitialised(*_a, **_kw):
        raise RuntimeError("ChannelRegistry not initialized")

    monkeypatch.setattr(registry_mod, "get_channel_registry", _uninitialised)


# ── the bots, and the room they are all in ────────────────────────────────


class _NativeAdapter(RoomAdapter):
    """A sibling whose transport hands it other bots' posts already.

    A Telegram userbot is a real account: it is in the room like any other
    member and receives everything posted there. The flag is read off the CLASS
    because it describes the transport, not one channel's configuration, so
    swapping the class is how a test says "this one is a userbot".
    """

    receives_bot_posts = True


class _RealForwarderAdapter(RoomAdapter):
    """The harness stubs ``_forward_reply``; the end-to-end tests need it real.

    Everything else about the harness adapter — the recorded sends, the fake
    storage — is worth keeping, so only the reply path is put back.
    """

    _forward_reply = BaseChannelAdapter._forward_reply


def _bot(channel_id, profile, self_id, username, display_name, **kw):
    """One Cremind Telegram bot channel, named the way the room sees it."""
    adapter = make_adapter(
        channel_id=channel_id, profile=profile, self_id=self_id, **kw,
    )
    adapter.channel["state"]["self_identity"].update({
        "username": username,
        "mention": f"@{username}",
        "display_name": display_name,
    })
    return adapter


def _cremind(**kw):
    """The channel that speaks first — ``admin``'s bot, as in the report."""
    return _bot("ch-1", "admin", "bot-1", "cremind_bot", "Cremind", **kw)


def _javis(**kw):
    """The other profile's bot, in the same group and hearing nothing."""
    return _bot("ch-2", "bobo", "bot-2", "javis_1511_bot", "Javis", **kw)


def _kit(**kw):
    """A third bot, for the tests about one sibling failing the others."""
    return _bot("ch-3", "kit", "bot-3", "kit_bot", "Kit", **kw)


def _speaking(adapter):
    """Swap the real reply forwarder in for the tests that drive one."""
    adapter.__class__ = _RealForwarderAdapter
    return adapter


async def _row(
    group_storage, adapter, *, status="approved",
    chat_id="-1001", title="AI-group-Cremind", **settings,
):
    """This channel's own ``channel_groups`` row for the shared room.

    Created directly rather than through discovery, so nothing here raises the
    operator notification that the phantom-group test below asserts the absence
    of. ``chat_id`` is spelled out for the tests about a channel that is in more
    than one room at once — one room is the default because that is what every
    other test here is about.
    """
    group = await group_storage.create_group(
        channel_id=adapter.channel_id,
        profile=adapter.profile,
        platform_chat_id=chat_id,
        title=title,
        chat_type="supergroup",
    )
    await group_storage.update_group(
        group["id"], status=status, settings=settings or None,
    )
    return await group_storage.get_group(group["id"])


async def _post(sender, sender_row, text=_HELLO):
    """Relay one post the way ``_forward_reply`` does once it really went out."""
    return await relay_agent_post(sender, group_target(sender_row), text)


# ── driving the real reply forwarder ──────────────────────────────────────


async def _forward(adapter, frames, *, target, response_mode="normal"):
    """Run the real ``_forward_reply`` over scripted frames, relays included.

    Adapted from ``test_dispatch_and_forwarder``'s ``_run_forward``, with the
    one difference that matters here: the relay is deliberately a TASK, because
    it does a database lookup and the reply path must not pay for it. A scenario
    that returned at the end of ``_forward_reply`` would leave that task pending,
    ``asyncio.run`` would destroy it on the way out, and the test would then be
    asserting against a sibling nothing was ever handed to. The whole in-flight
    set is drained rather than one tail, because the chains are per room: a turn
    that spoke in two rooms has two tails and awaiting either would prove nothing
    about the other.
    """
    adapter.channel["response_mode"] = response_mode

    class _Bus:
        async def subscribe(self, _conversation_id):
            return None, list(frames), False

        async def unsubscribe(self, _conversation_id, _queue):
            return None

    import app.channels.base as base_mod

    original = base_mod.get_event_stream_bus
    base_mod.get_event_stream_bus = lambda: _Bus()
    try:
        await adapter._forward_reply("conv-1", target)
    finally:
        base_mod.get_event_stream_bus = original
    await _drain_relays(adapter)


async def _drain_relays(adapter) -> None:
    """Wait for every relay this adapter still has in flight."""
    relays = list(adapter._relay_tasks)
    if relays:
        await asyncio.gather(*relays, return_exceptions=True)


def _text(token, seq: int = 2):
    # Distinct seqs matter: the forwarder dedupes on them, so two frames sharing
    # one would silently swallow the second.
    return {"seq": seq, "type": "text", "data": {"token": token}}


def _thinking(action_input="kubectl get pods", seq: int = 1):
    return {"seq": seq, "type": "thinking",
            "data": {"Thought": "checking", "Action": "shell",
                     "Action_Input": action_input}}


def _flow_break(seq: int = 3):
    return {"seq": seq, "type": "flow_break", "data": {"message_ids": ["m1"]}}


def _complete(assistant_id="a-1"):
    return {"seq": 9, "type": "complete", "data": {"assistant_id": assistant_id}}


# ── what the sibling is handed ────────────────────────────────────────────


def test_a_post_arrives_at_the_sibling_as_the_bot_that_wrote_it(
    group_storage, delivery, monkeypatch,
):
    """The whole feature in one case.

    Javis is handed what Telegram refused to hand it, attributed to Cremind
    rather than to nobody, in Javis's own conversation for the room — and
    because the text says ``@javis_1511_bot`` it answers instead of filing the
    message away and staying quiet.
    """
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        assert await _post(cremind, mine) == 1

    asyncio.run(scenario())

    (call,) = delivery["enqueued"]
    assert call["profile"] == "bobo"
    assert call["query"] == f"Cremind (@cremind_bot): {_HELLO}"
    metadata = call["user_message_metadata"]
    assert metadata["channel_id"] == "ch-2"
    stamp = metadata["channel_group"]
    assert stamp["sender_is_bot"] is True
    assert stamp["sender_id"] == "bot-1"
    assert stamp["sender_username"] == "cremind_bot"
    assert stamp["display_name"] == "Cremind"
    assert stamp["decision"] == "mentioned"

    # The room's transcript belongs to the sibling, not to the channel that
    # spoke: the sender already has this turn in its own conversation.
    (conv,) = javis.storage.conversations.values()
    assert conv["channel_id"] == "ch-2"
    assert conv["context_id"].startswith("channel_group:")
    assert cremind.storage.conversations == {}


def test_a_relayed_post_the_sibling_stays_quiet_on_is_stored_as_bot_authored(
    group_storage, delivery, monkeypatch,
):
    """Stored, and stored as what it is.

    A relayed post that does not address the sibling is context like any other
    message — but the row has to carry ``sender_is_bot``, because that flag is
    what the consecutive-bot brake counts and what tells the agent it is reading
    another assistant rather than a person.
    """
    _judge(monkeypatch, False)
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _post(cremind, mine, "Morning everyone.")

    asyncio.run(scenario())

    assert delivery["enqueued"] == []
    (row,) = javis.storage.messages
    assert row["content"] == "Cremind (@cremind_bot): Morning everyone."
    stamp = row["metadata"]["channel_group"]
    assert stamp["sender_is_bot"] is True
    assert stamp["decision"] == "judge:irrelevant"
    assert stamp["quiet"] is True


def test_the_sending_bot_shows_up_in_the_siblings_member_list(
    group_storage, delivery, monkeypatch,
):
    """A relayed post is a real message from a real account in the room.

    It matters more here than anywhere else: a Telegram bot cannot enumerate a
    group's members, so whoever has posted is the only member list it will ever
    have — and the member policy is written against that list.
    """
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        theirs = await _row(group_storage, javis)
        await _post(cremind, mine)
        return await group_storage.list_members(theirs["id"])

    (member,) = asyncio.run(scenario())
    assert member["member_id"] == "bot-1"
    assert member["display_name"] == "Cremind"
    assert member["username"] == "cremind_bot"
    assert member["is_bot"] is True


def test_the_answer_the_room_read_is_the_answer_the_sibling_hears(
    group_storage, delivery, monkeypatch,
):
    """End to end through the real reply forwarder.

    Everything above calls the relay directly; this pins that the forwarder
    actually calls it, and that what reaches Telegram and what reaches the other
    bot are one and the same post.
    """
    cremind, javis = _speaking(_cremind()), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _forward(
            cremind, [_text(_HELLO), _complete()], target=group_target(mine),
        )

    asyncio.run(scenario())
    assert cremind.chat_sends == [("-1001", _HELLO)]
    (call,) = delivery["enqueued"]
    assert call["profile"] == "bobo"
    assert call["query"] == f"Cremind (@cremind_bot): {_HELLO}"


def test_an_interim_reply_reaches_the_sibling_before_the_final_answer(
    group_storage, delivery, monkeypatch,
):
    """A turn cut mid-flight speaks twice, and order is not free.

    Each relay does an async lookup before it delivers anything, so two fired
    off independently would race on it and the sibling could read "Done." before
    "Not yet." — an agent answering a question that had already been answered.
    The room's FIFO chain in ``_relay_group_post`` is what stops that.
    """
    _judge(monkeypatch, False)
    cremind, javis = _speaking(_cremind()), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _forward(
            cremind,
            [
                _text("Not yet.", seq=2),
                _flow_break(),
                _text("Done.", seq=4),
                _complete(),
            ],
            target=group_target(mine),
        )

    asyncio.run(scenario())
    assert cremind.chat_sends == [("-1001", "Not yet."), ("-1001", "Done.")]
    assert [row["content"] for row in javis.storage.messages] == [
        "Cremind (@cremind_bot): Not yet.",
        "Cremind (@cremind_bot): Done.",
    ]


def test_reasoning_steps_and_the_response_header_stay_out_of_the_relay(
    group_storage, delivery, monkeypatch,
):
    """The sibling hears what the agent SAID, not how Cremind dressed it up.

    A reasoning step is scaffolding around an answer and the detail-mode
    "*Response*" prefix is presentation for the room's human readers. A sibling
    agent handed either would be reading our own plumbing back as somebody's
    words — and the steps would spend its brakes on top.
    """
    _judge(monkeypatch, False)
    cremind, javis = _speaking(_cremind()), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _forward(
            cremind,
            [_thinking(), _text("all green", seq=2), _complete()],
            target=group_target(mine),
            response_mode="detail",
        )

    asyncio.run(scenario())
    # The room really did get both bubbles, header and all.
    step, answer = cremind.chat_sends
    assert "kubectl get pods" in step[1]
    assert answer[1].startswith("*Response*")
    # The sibling got exactly one message, and it is the bare answer.
    (row,) = javis.storage.messages
    assert row["content"] == "Cremind (@cremind_bot): all green"


# ── who is deliberately not told ──────────────────────────────────────────


def test_the_channel_that_spoke_is_never_handed_its_own_post(
    group_storage, delivery, monkeypatch,
):
    """It would come straight back in and the agent would answer itself.

    The echo filter would catch it a step later, under our own account id, but
    the relay must not get that far: this is the same room and the same account,
    and there is nothing to deliver.
    """
    cremind = _cremind()
    _registry(monkeypatch, cremind)

    async def scenario():
        mine = await _row(group_storage, cremind)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    assert cremind.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_sibling_the_platform_already_delivers_to_is_left_alone(
    group_storage, delivery, monkeypatch,
):
    """A userbot is a real account: Telegram hands it every post in the room,
    ours included. Relaying would give it a second copy of a message it already
    has, which reads as the other agent saying the same thing twice."""
    cremind, userbot = _cremind(), _javis()
    userbot.__class__ = _NativeAdapter
    _registry(monkeypatch, cremind, userbot)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, userbot)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    assert userbot.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_sibling_with_group_chats_switched_off_hears_nothing(
    group_storage, delivery, monkeypatch,
):
    """Off means off, exactly as it does for a message the platform DID deliver.

    A channel that has not opted into group chats must not learn the room exists
    — and an in-process relay is the one path that could sneak past the switch,
    because it does not come from the platform at all.
    """
    cremind, javis = _cremind(), _javis(enabled=False)
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_post_from_a_real_account_needs_no_relay(
    group_storage, delivery, monkeypatch,
):
    """The rule is about bots, not about Cremind.

    When the posting channel is a userbot, the platform delivers its message to
    everyone in the room, siblings included. Relaying it as well would hand them
    a duplicate — so "are we posting as a bot?" is asked before anything else.
    """
    cremind, javis = _cremind(), _javis()
    cremind.channel["state"]["self_identity"]["is_bot"] = False
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_sibling_with_no_row_for_this_chat_is_not_woken(
    group_storage, notifications, delivery, monkeypatch,
):
    """The phantom-pending-group regression, and the reason the relay reads rows
    at all rather than just handing the post to every live sibling.

    Javis is running and in group chats, but nobody added it to this room, so it
    has no row. Delivering to it would push the message through the discovery
    path and manufacture a *pending* group plus a "approve this?" notification
    for a chat Telegram never told that channel about. The real row is created
    by Telegram's own ``my_chat_member`` event when the bot is genuinely added,
    and that is the one an operator should be answering.
    """
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    assert [row["channel_id"] for row in group_storage.groups.values()] == ["ch-1"]
    assert notifications == []
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_a_sibling_whose_room_is_not_approved_hears_nothing(
    group_storage, delivery, monkeypatch, status,
):
    """Both statuses are a decision somebody made about this exact group.

    Pending means the operator has not answered yet; blocked means they said no.
    Neither is an invitation to start delivering to it from another channel —
    the approval gate exists precisely so that nothing reaches an agent in a
    room before a human has said it may.
    """
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis, status=status)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_post_does_not_cross_platforms(
    group_storage, delivery, monkeypatch,
):
    """Chat ids are only unique within a platform.

    The candidates come from one platform's adapters, so a Discord channel whose
    room happens to be numbered "-1001" too is never even considered — and it is
    never considered *before* the row lookup, which is what stops a Telegram post
    landing in a Discord transcript on the strength of a coincidence.
    """
    cremind, discord = _cremind(), _javis()
    discord.channel["channel_type"] = "discord"
    registry = _registry(monkeypatch, cremind, discord)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, discord)
        assert await _post(cremind, mine) == 0

    asyncio.run(scenario())
    # The scoping is the relay's to ask for, not the registry's to guess.
    assert registry.asked == ["telegram"]
    assert discord.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_turn_that_stays_silent_relays_nothing(
    group_storage, delivery, monkeypatch,
):
    """Silence is the agent deciding the message was not for it, which in a room
    with two agents is most messages. Nothing went out to the room, so nothing
    is owed to the siblings either — and relaying the sentinel as text would put
    a literal "[silent]" in the other agent's transcript."""
    cremind, javis = _speaking(_cremind()), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _forward(
            cremind, [_text("[silent]"), _complete()], target=group_target(mine),
        )
        assert cremind._relay_tails == {}

    asyncio.run(scenario())
    assert cremind.chat_sends == []
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


def test_a_post_the_platform_refused_is_never_handed_to_a_sibling(
    group_storage, delivery, monkeypatch,
):
    """The room did not hear it, so nobody may be told it did.

    ``send_to_chat_chunked`` swallows a transport error on purpose — one failed
    bubble must not abandon the rest of a multi-bubble message — which used to
    mean the reply path could not tell a delivered post from a refused one, and
    relayed both. A bot that is rate-limited (429) or has had "send messages"
    taken away in a Telegram group hits exactly that: the send raises, the room
    sees nothing, and the sibling would be handed a line that normally names it
    by handle, answer it out loud, and leave the people in the room reading a
    reply to a message that was never posted. It would keep doing so for as long
    as the restriction stood.

    The flood counter still counts the attempt: it is a brake, a refused send
    still spent the transport's budget, and over-counting a brake errs towards
    quiet.
    """
    cremind, javis = _speaking(_cremind()), _javis()
    _registry(monkeypatch, cremind, javis)

    async def _refused(_chat_id, _text):
        raise RuntimeError("Forbidden: have no rights to send a message")

    cremind.send_to_chat = _refused

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _forward(
            cremind, [_text(_HELLO), _complete()], target=group_target(mine),
        )
        return cremind.groups.agent_posts_last_minute(mine["id"])

    assert asyncio.run(scenario()) == 1
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


def test_one_rooms_slow_sibling_does_not_hold_up_another_rooms_relay(
    group_storage, delivery, monkeypatch,
):
    """The FIFO chain is per room, and this is why it has to be.

    A relay awaits the sibling's whole inbound pipeline — its roster refresh and
    its relevance judge, which is an LLM call with a twelve-second timeout —
    while that sibling holds its own lock for the room. Chaining every room's
    relays together would park the standup room's post behind the busy room's
    sibling. That is worse than late: a person posting in the standup room is
    delivered natively and at once, so the sibling would read the answer before
    the message it answers — the inversion the chain exists to prevent, put back
    across rooms.
    """
    _judge(monkeypatch, False)
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    busy = asyncio.Event()
    standup_arrived = asyncio.Event()
    arrived: list[str] = []
    deliver = javis._handle_group_inbound

    async def _paced(**kw):
        if kw["chat_id"] == "-1001":
            await busy.wait()
        arrived.append(kw["chat_id"])
        if kw["chat_id"] == "-1002":
            standup_arrived.set()
        await deliver(**kw)

    javis._handle_group_inbound = _paced

    async def scenario():
        first = await _row(group_storage, cremind)
        second = await _row(
            group_storage, cremind, chat_id="-1002", title="Standup",
        )
        await _row(group_storage, javis)
        await _row(group_storage, javis, chat_id="-1002", title="Standup")

        cremind._relay_group_post(group_target(first), "Working on it.")
        cremind._relay_group_post(group_target(second), "Morning.")
        # One chain per adapter and this never fires: the standup relay would
        # still be waiting on the busy room's sibling.
        await asyncio.wait_for(standup_arrived.wait(), timeout=5)
        busy.set()
        await _drain_relays(cremind)

    asyncio.run(scenario())
    assert arrived == ["-1002", "-1001"]
    assert [row["content"] for row in javis.storage.messages] == [
        "Cremind (@cremind_bot): Morning.",
        "Cremind (@cremind_bot): Working on it.",
    ]


def test_stopping_the_sender_does_not_abort_the_siblings_handling(
    group_storage, delivery, monkeypatch,
):
    """A relay runs as the SENDER's task but does the RECEIVER's work.

    ``stop()`` cancels this adapter's relays, and rightly: a relay still waiting
    at shutdown would post into a room this channel no longer serves. But an
    operator disabling or editing one channel — or its receive loop dying — must
    not reach into another profile's live adapter and abort a message halfway
    through, between storing the row and starting the turn. Nothing would record
    it either: the dedupe ring has already claimed the message, so it could never
    be reconsidered, and a half-registered run leaves a forwarder subscribed to a
    turn that never comes. The delivery is shielded and owned by the sibling, so
    it finishes where it belongs.
    """
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    entered = asyncio.Event()
    release = asyncio.Event()
    deliver = javis._handle_group_inbound

    async def _slow(**kw):
        entered.set()
        await release.wait()
        await deliver(**kw)

    javis._handle_group_inbound = _slow

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        cremind._relay_group_post(group_target(mine), _HELLO)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await cremind.stop()
        # The sender is gone and took its own relay with it.
        assert cremind._relay_tasks == set()
        release.set()
        await _drain_relays(javis)

    asyncio.run(scenario())
    (call,) = delivery["enqueued"]
    assert call["profile"] == "bobo"
    assert call["query"] == f"Cremind (@cremind_bot): {_HELLO}"


def test_without_a_channel_registry_nothing_is_relayed_and_nothing_hangs(
    group_storage, delivery, monkeypatch,
):
    """Every process that answers a room without a channel subsystem behind it.

    ``relay_candidates`` has to answer "nobody" synchronously here. Spawning a
    task that then discovered there was no registry would leave one
    destroyed-pending task behind per post — on every install, including the
    single-bot ones this feature is invisible to.
    """
    _no_registry(monkeypatch)
    cremind, javis = _speaking(_cremind()), _javis()

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        target = group_target(mine)
        assert relay_candidates(cremind, target) == []
        # Must not raise: the room already has the message, and a relay that
        # cannot even look for siblings is not the caller's problem.
        assert await relay_agent_post(cremind, target, _HELLO) == 0
        await _forward(cremind, [_text(_HELLO), _complete()], target=target)
        assert cremind._relay_tails == {}
        assert cremind._relay_tasks == set()
        assert [
            task for task in asyncio.all_tasks()
            if task.get_name().startswith("channel-relay:")
        ] == []

    asyncio.run(scenario())
    assert cremind.chat_sends == [("-1001", _HELLO)]
    assert javis.storage.messages == []
    assert delivery["enqueued"] == []


# ── the brakes, which are what bound the exchange ─────────────────────────


def test_relayed_posts_count_towards_the_siblings_bot_streak(
    group_storage, delivery, notifications, monkeypatch,
):
    """The brake is the only thing between this feature and a token loop.

    Two agents that can finally hear each other will keep being helpful at each
    other until a person speaks, so a relayed post has to count towards the
    consecutive-bot streak exactly as a third-party bot's would. Braked means
    quiet, not blind: the messages are still stored, so the agent knows what was
    said while it was holding back and one notification says it is holding back.
    """
    cremind, javis = _cremind(), _javis()
    _registry(monkeypatch, cremind, javis)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis, max_consecutive_bot_messages=2)
        for round_ in ("one", "two", "three"):
            await _post(cremind, mine, f"@javis_1511_bot round {round_}")

    asyncio.run(scenario())

    # The first is answered; the streak reaches the cap and the rest are not.
    (call,) = delivery["enqueued"]
    assert call["query"].endswith("round one")
    assert [
        row["metadata"]["channel_group"]["decision"] for row in javis.storage.messages
    ] == ["brake:bots", "brake:bots"]
    (note,) = notifications
    assert note["extra"]["brake"] == "bots"
    assert note["profile"] == "bobo"


# ── one sibling failing ───────────────────────────────────────────────────


def test_one_sibling_failing_does_not_cost_the_others_their_copy(
    group_storage, delivery, monkeypatch,
):
    """Three of your own bots in one room is a supported setup.

    From inside the room a sibling that never got the post is indistinguishable
    from an agent that chose to stay quiet, so one broken delivery must not
    spread that to the rest — and it must not reach the forwarder either, whose
    post has already gone out to the people in the room.
    """
    _judge(monkeypatch, False)
    cremind, javis, kit = _speaking(_cremind()), _javis(), _kit()

    async def _boom(**_kw):
        raise RuntimeError("this channel's storage is on fire")

    javis._handle_group_inbound = _boom
    _registry(monkeypatch, cremind, javis, kit)

    async def scenario():
        mine = await _row(group_storage, cremind)
        await _row(group_storage, javis)
        await _row(group_storage, kit)
        await _forward(
            cremind, [_text(_HELLO), _complete()], target=group_target(mine),
        )

    asyncio.run(scenario())  # must not raise
    assert cremind.chat_sends == [("-1001", _HELLO)]
    assert [row["content"] for row in kit.storage.messages] == [
        f"Cremind (@cremind_bot): {_HELLO}",
    ]


# ── helpers ───────────────────────────────────────────────────────────────


def _judge(monkeypatch, verdict) -> None:
    """Pin the relevance judge's answer without going near an LLM.

    Every relayed post that does not name the receiving bot ends up here, and a
    fixed "no" is what makes the sibling STORE the message instead of answering
    it — which is where these tests can read what it was handed.
    """
    import app.channels.groups.judge as judge_mod

    async def _fn(**_kw):
        return verdict

    monkeypatch.setattr(judge_mod, "judge_relevance", _fn)
