"""The decision every inbound group message goes through.

Most of what a real group produces is not for the agent, so most of what is
pinned here is the agent NOT answering — and, separately, the agent still
knowing what was said. The two are different outcomes and a test that only
checked "no turn started" would pass on a pipeline that had thrown the message
away.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels.groups.inbound import (
    GroupInbound, handle_group_joined, handle_group_message,
)
from tests.channels.groups.conftest import make_adapter


def _msg(**overrides) -> GroupInbound:
    base = {
        "chat_id": "-1001",
        "chat_title": "Ops room",
        "chat_type": "supergroup",
        "sender_id": "u-alexa",
        "sender_username": "alexa",
        "display_name": "Alexa Nguyen",
        "text": "what is the deploy status?",
        "platform_message_id": "m-1",
        "sender_is_bot": False,
    }
    base.update(overrides)
    return GroupInbound(**base)


async def _approved(adapter, group_storage, **settings):
    """A group that has already been approved, with the pipeline's own row."""
    group = await group_storage.create_group(
        channel_id=adapter.channel_id, profile=adapter.profile,
        platform_chat_id="-1001", title="Ops room", chat_type="supergroup",
    )
    await group_storage.update_group(
        group["id"], status="approved", settings=settings or None,
    )
    return group["id"]


# ── the feature switch ────────────────────────────────────────────────────


def test_a_channel_with_group_chats_off_learns_nothing(
    group_storage, notifications, delivery,
):
    """Off means off: no row, no notification, nothing stored.

    The strongest promise the feature makes. A bot can be pulled into any group
    by anyone, so a channel that has not opted in must not even record that it
    happened.
    """
    adapter = make_adapter(enabled=False)
    asyncio.run(handle_group_message(adapter, _msg()))

    assert group_storage.groups == {}
    assert notifications == []
    assert delivery["enqueued"] == []
    assert adapter.storage.messages == []


# ── discovery ─────────────────────────────────────────────────────────────


def test_an_unknown_group_becomes_pending_and_asks(
    group_storage, notifications, delivery,
):
    adapter = make_adapter()
    asyncio.run(handle_group_message(adapter, _msg()))

    (group,) = group_storage.groups.values()
    assert group["status"] == "pending"
    assert group["discovered_via"] == "message"
    assert group["title"] == "Ops room"

    (note,) = notifications
    assert note["kind"] == "channel_group_request"
    assert note["priority"] == "high"
    # No conversation id: the notification asks for a DECISION, and a click that
    # opened a transcript instead of the Channels page would be a dead end.
    assert note["conversation_id"] == ""
    assert note["extra"]["group_id"] == group["id"]
    assert note["extra"]["channel_id"] == "ch-1"

    # Nothing is delivered to an agent, and the message itself is dropped.
    assert delivery["enqueued"] == []
    assert adapter.storage.messages == []


def test_a_second_message_from_a_pending_group_asks_only_once(
    group_storage, notifications, delivery,
):
    """The row is the durable "already asked" marker.

    An in-memory set would re-ask after every restart, and a notification per
    restart for a group nobody got round to is how a notification list stops
    being read.
    """
    adapter = make_adapter()
    asyncio.run(handle_group_message(adapter, _msg(platform_message_id="m-1")))
    asyncio.run(handle_group_message(adapter, _msg(platform_message_id="m-2")))

    assert len(group_storage.groups) == 1
    assert len(notifications) == 1


def test_a_blocked_group_is_silent_and_stores_nothing(
    group_storage, notifications, delivery,
):
    adapter = make_adapter()

    async def _run():
        group = await group_storage.create_group(
            channel_id="ch-1", profile="admin", platform_chat_id="-1001",
        )
        await group_storage.update_group(group["id"], status="blocked")
        notifications.clear()
        await handle_group_message(adapter, _msg())

    asyncio.run(_run())
    assert delivery["enqueued"] == []
    assert adapter.storage.messages == []
    assert notifications == []


def test_being_added_to_a_group_asks_before_anybody_speaks(
    group_storage, notifications,
):
    """The whole point of the join event: on a platform that reports one, the
    operator is asked while the group is still empty."""
    adapter = make_adapter()
    asyncio.run(handle_group_joined(
        adapter, chat_id="-1002", chat_title="New room", chat_type="supergroup",
    ))

    (group,) = group_storage.groups.values()
    assert group["status"] == "pending"
    assert group["discovered_via"] == "join"
    assert len(notifications) == 1


# ── echo and dedupe ───────────────────────────────────────────────────────


@pytest.mark.parametrize("sender_id,alt_ids", [
    ("bot-1", []),
    ("someone-else", ["bot-1"]),
])
def test_our_own_message_never_comes_back_in(
    group_storage, notifications, delivery, sender_id, alt_ids,
):
    """Under either of our ids. On a platform with no bot flag this is the only
    echo defence there is, and getting it wrong makes the room answer itself."""
    adapter = make_adapter()
    asyncio.run(handle_group_message(
        adapter, _msg(sender_id=sender_id, sender_alt_ids=alt_ids),
    ))
    assert group_storage.groups == {}


def test_one_message_seen_twice_is_handled_once(
    group_storage, notifications, delivery,
):
    adapter = make_adapter()

    async def _run():
        gid = await _approved(adapter, group_storage)
        for _ in range(2):
            await handle_group_message(adapter, _msg(mentioned=True))
        return gid

    asyncio.run(_run())
    assert len(delivery["enqueued"]) == 1


def test_a_legacy_telegram_group_dedupes_on_content_and_time(
    group_storage, delivery,
):
    """Legacy groups number messages per ACCOUNT, so the ids disagree and cannot
    identify a message. The fingerprint has to settle it instead — including the
    send time, or the second time anyone said "status?" it would be swallowed."""
    adapter = make_adapter()

    async def _run():
        await _approved(adapter, group_storage)
        common = {"chat_type": "group", "text": "status?", "mentioned": True}
        # Same message, different per-account ids → one turn.
        await handle_group_message(
            adapter, _msg(platform_message_id="7", platform_message_date=100, **common),
        )
        await handle_group_message(
            adapter, _msg(platform_message_id="9", platform_message_date=100, **common),
        )
        # Said again later → a real second message.
        await handle_group_message(
            adapter, _msg(platform_message_id="11", platform_message_date=200, **common),
        )

    asyncio.run(_run())
    assert len(delivery["enqueued"]) == 2


# ── who gets answered ─────────────────────────────────────────────────────


def test_a_mention_starts_a_turn_immediately(group_storage, delivery):
    adapter = make_adapter()

    asyncio.run(_drive(adapter, group_storage, _msg(mentioned=True)))

    (call,) = delivery["enqueued"]
    assert call["profile"] == "admin"
    assert "Alexa Nguyen (@alexa): what is the deploy status?" in call["query"]
    stamp = call["user_message_metadata"]["channel_group"]
    assert stamp["decision"] == "mentioned"
    assert stamp["quiet"] is False


def test_an_unmentioned_message_the_judge_likes_starts_a_turn(
    group_storage, delivery, monkeypatch,
):
    _judge(monkeypatch, True)
    adapter = make_adapter()
    asyncio.run(_drive(adapter, group_storage, _msg()))

    (call,) = delivery["enqueued"]
    assert call["user_message_metadata"]["channel_group"]["decision"] == "judge:relevant"


def test_an_unmentioned_message_the_judge_rejects_is_kept_as_context(
    group_storage, delivery, monkeypatch,
):
    """Stored, not dropped. The agent has to know what the room has been talking
    about by the time somebody does address it — otherwise it answers the
    question it was asked with none of the thread behind it."""
    _judge(monkeypatch, False)
    adapter = make_adapter()
    asyncio.run(_drive(adapter, group_storage, _msg()))

    assert delivery["enqueued"] == []
    (row,) = adapter.storage.messages
    assert row["role"] == "user"
    assert "Alexa Nguyen (@alexa)" in row["content"]
    assert row["metadata"]["channel_group"]["decision"] == "judge:irrelevant"
    assert row["metadata"]["channel_group"]["quiet"] is True


def test_a_judge_that_raises_starts_no_turn_and_ends_no_loop(
    group_storage, delivery, monkeypatch,
):
    """The judge is built to fail closed on its own (see ``test_judge.py``), so
    an exception reaching here means something unforeseen — and the receive loop
    still has to survive it, quietly."""
    _judge(monkeypatch, None, raises=True)

    adapter = make_adapter()
    asyncio.run(_drive(adapter, group_storage, _msg()))  # must not raise

    assert delivery["enqueued"] == []


def test_mention_only_never_asks_the_judge(group_storage, delivery, monkeypatch):
    calls: list = []

    async def _judge_fn(**kw):
        calls.append(kw)
        return True

    import app.channels.groups.judge as judge_mod
    monkeypatch.setattr(judge_mod, "judge_relevance", _judge_fn)

    adapter = make_adapter()
    asyncio.run(_drive(
        adapter, group_storage, _msg(), settings={"respond_mode": "mention_only"},
    ))

    assert calls == []
    assert delivery["enqueued"] == []


# ── member policy ─────────────────────────────────────────────────────────


def test_a_denied_member_is_dropped_entirely(group_storage, delivery):
    """Not even stored as context: somebody blocked should not be able to fill
    the agent's history either."""
    adapter = make_adapter()
    asyncio.run(_drive(
        adapter, group_storage, _msg(mentioned=True),
        settings={"member_policy": {
            "mode": "everyone", "allow": [], "deny": ["u-alexa"],
        }},
    ))

    assert delivery["enqueued"] == []
    assert adapter.storage.messages == []


def test_selected_mode_answers_only_the_allow_list(group_storage, delivery):
    adapter = make_adapter()
    settings = {"member_policy": {
        "mode": "selected", "allow": ["u-boss"], "deny": [],
    }}

    async def _run():
        gid = await _approved(adapter, group_storage, **settings)
        await handle_group_message(adapter, _msg(mentioned=True))
        await handle_group_message(adapter, _msg(
            sender_id="u-boss", display_name="The Boss",
            platform_message_id="m-2", mentioned=True,
        ))
        return gid

    asyncio.run(_run())
    (call,) = delivery["enqueued"]
    assert "The Boss" in call["query"]


def test_a_denied_member_is_matched_under_any_of_their_ids(
    group_storage, delivery,
):
    """WhatsApp reports one account as two JIDs; a deny list written against
    whichever one the operator had in front of them has to match the other."""
    adapter = make_adapter()
    asyncio.run(_drive(
        adapter, group_storage,
        _msg(sender_id="99@lid", sender_alt_ids=["1555@s.whatsapp.net"],
             mentioned=True),
        settings={"member_policy": {
            "mode": "everyone", "allow": [], "deny": ["1555@s.whatsapp.net"],
        }},
    ))
    assert delivery["enqueued"] == []


# ── loop brakes ───────────────────────────────────────────────────────────


def test_the_rate_cap_stops_the_agent_but_not_its_memory(
    group_storage, delivery, notifications,
):
    adapter = make_adapter()

    async def _run():
        gid = await _approved(
            adapter, group_storage, max_agent_posts_per_minute=2,
        )
        for _ in range(2):
            adapter.groups.note_agent_post(gid)
        notifications.clear()
        await handle_group_message(adapter, _msg(mentioned=True))

    asyncio.run(_run())
    assert delivery["enqueued"] == []
    (row,) = adapter.storage.messages
    assert row["metadata"]["channel_group"]["decision"] == "brake:rate"


def test_a_wall_of_bot_messages_quiets_the_agent_until_a_person_speaks(
    group_storage, delivery,
):
    """Two assistants being endlessly helpful at each other is the failure this
    exists for; a human joining in is what says the conversation is real."""
    adapter = make_adapter()

    async def _run():
        gid = await _approved(
            adapter, group_storage, max_consecutive_bot_messages=2,
        )
        for _ in range(2):
            adapter.groups.note_inbound_author(gid, True)
        await handle_group_message(adapter, _msg(
            sender_is_bot=True, mentioned=True, platform_message_id="b-1",
        ))
        assert delivery["enqueued"] == []
        # A person speaks: the streak resets and the next message is answered.
        adapter.groups.note_inbound_author(gid, False)
        await handle_group_message(adapter, _msg(
            mentioned=True, platform_message_id="h-1",
        ))

    asyncio.run(_run())
    assert len(delivery["enqueued"]) == 1


# ── delivery plumbing ─────────────────────────────────────────────────────


def test_a_message_folded_into_a_running_turn_starts_no_second_one(
    group_storage, delivery,
):
    from app.events.user_message_delivery import ParkOutcome

    delivery["parked"] = ParkOutcome(injected=True, message_id="m-parked")
    adapter = make_adapter()

    async def _run():
        await _drive_async(adapter, group_storage, _msg(mentioned=True))
        assert delivery["enqueued"] == []
        # A forwarder is still ensured: the running turn may have been started
        # somewhere that pointed none at this room, and then nothing would
        # carry its answer out.
        keys = [k for k in adapter._inflight if k.startswith("cg:")]
        assert keys
        # Tear it down inside the loop it was spawned on — a live task at
        # teardown is a "loop is closed" traceback in an unrelated test.
        await adapter.stop()

    asyncio.run(_run())


def test_a_group_gets_one_conversation_and_keeps_it(group_storage, delivery):
    adapter = make_adapter()

    async def _run():
        await _approved(adapter, group_storage)
        await handle_group_message(adapter, _msg(mentioned=True))
        await handle_group_message(
            adapter, _msg(mentioned=True, platform_message_id="m-2"),
        )

    asyncio.run(_run())
    assert len(adapter.storage.conversations) == 1
    (conv,) = adapter.storage.conversations.values()
    assert conv["context_id"].startswith("channel_group:")
    assert conv["kind"] == "chat"
    assert conv["channel_id"] == "ch-1"
    assert conv["title"] == "Ops room"


def test_a_renamed_group_renames_its_conversation(group_storage, delivery):
    adapter = make_adapter()

    async def _run():
        await _approved(adapter, group_storage)
        await handle_group_message(adapter, _msg(mentioned=True))
        await handle_group_message(adapter, _msg(
            chat_title="Ops room (EU)", mentioned=True, platform_message_id="m-2",
        ))

    asyncio.run(_run())
    (conv,) = adapter.storage.conversations.values()
    assert conv["title"] == "Ops room (EU)"


# ── being addressed by name, where the platform reports nothing ───────────


def _named_adapter(**kw):
    """An adapter whose account shows up in the group as "Rex Nguyen"."""
    adapter = make_adapter(**kw)
    adapter.channel["state"]["self_identity"]["display_name"] = "Rex Nguyen"
    return adapter


@pytest.mark.parametrize("text", [
    "Hello @Rex Nguyen how are you?",
    "Rex Nguyen, what time is it?",
    "rex nguyen what time is it?",
])
def test_being_named_in_the_text_counts_as_a_mention(
    group_storage, delivery, monkeypatch, text,
):
    """On Zalo a typed "@Rex Nguyen" is not a structured mention — the platform
    reports nothing — but the person typing it plainly meant to address the
    agent. Answering only structured mentions there meant answering nothing.
    """
    _judge(monkeypatch, False)  # the judge would have said no; the name wins
    adapter = _named_adapter()
    asyncio.run(_drive(adapter, group_storage, _msg(text=text)))

    (call,) = delivery["enqueued"]
    assert call["user_message_metadata"]["channel_group"]["decision"] == "mentioned"


@pytest.mark.parametrize("text", [
    "ask Rex Nguyen about it later",       # about the agent, not to it
    "Rex Nguyenson said hello",            # a different name that starts the same
    "Hello everyone",                      # nobody named at all
])
def test_merely_containing_the_name_is_left_to_the_judge(
    group_storage, delivery, monkeypatch, text,
):
    """The fallback is narrow on purpose: "@name" or the name opening the
    message. Anything looser and every mention of the agent in the third person
    would wake it."""
    _judge(monkeypatch, False)
    adapter = _named_adapter()
    asyncio.run(_drive(adapter, group_storage, _msg(text=text)))

    assert delivery["enqueued"] == []
    (row,) = adapter.storage.messages
    assert row["metadata"]["channel_group"]["decision"] == "judge:irrelevant"


def test_a_message_to_the_whole_group_is_answered_when_the_judge_says_so(
    group_storage, delivery, monkeypatch,
):
    """The reported bug, end to end: "Hello everyone, how are you?" reaches the
    judge, and a yes starts a turn like any mention would."""
    _judge(monkeypatch, True)
    adapter = _named_adapter()
    asyncio.run(_drive(
        adapter, group_storage, _msg(text="Hello everyone, how are you?"),
    ))

    (call,) = delivery["enqueued"]
    assert (
        call["user_message_metadata"]["channel_group"]["decision"]
        == "judge:relevant"
    )


def test_the_judge_is_told_the_account_name_and_the_other_members(
    group_storage, monkeypatch,
):
    """Both are what make "this is for me" and "this is for Sam" separable."""
    seen: dict = {}

    import app.channels.groups.judge as judge_mod

    async def _fn(**kw):
        seen.update(kw)
        return False

    monkeypatch.setattr(judge_mod, "judge_relevance", _fn)
    adapter = _named_adapter()

    async def _run():
        group_id = await _approved(adapter, group_storage)
        await group_storage.upsert_member(
            group_id, member_id="u-sam", display_name="Sam", source="roster",
        )
        await handle_group_message(adapter, _msg())

    asyncio.run(_run())
    assert seen["account_name"] == "Rex Nguyen"
    assert "Sam" in seen["members"]


def test_the_judge_is_never_handed_our_own_account_as_a_member(
    group_storage, monkeypatch,
):
    """A model told the agent is in the room's member list starts reading
    messages addressed to it as messages addressed to someone else."""
    seen: dict = {}

    import app.channels.groups.judge as judge_mod

    async def _fn(**kw):
        seen.update(kw)
        return False

    monkeypatch.setattr(judge_mod, "judge_relevance", _fn)
    adapter = _named_adapter()

    async def _run():
        group_id = await _approved(adapter, group_storage)
        await group_storage.upsert_member(
            group_id, member_id="bot-1", display_name="Rex Nguyen",
            source="roster",
        )
        await handle_group_message(adapter, _msg())

    asyncio.run(_run())
    assert "Rex Nguyen" not in seen["members"]


def test_a_failure_downstream_never_escapes_the_receive_loop(
    group_storage, monkeypatch,
):
    """The caller is a poll loop; one unroutable message must not end it."""
    async def _boom(*_a, **_kw):
        raise RuntimeError("storage is on fire")

    group_storage.get_group_by_chat = _boom
    adapter = make_adapter()
    asyncio.run(handle_group_message(adapter, _msg()))  # must not raise


# ── helpers ───────────────────────────────────────────────────────────────


async def _drive_async(adapter, group_storage, msg, settings=None):
    await _approved(adapter, group_storage, **(settings or {}))
    await handle_group_message(adapter, msg)


def _drive(adapter, group_storage, msg, settings=None):
    return _drive_async(adapter, group_storage, msg, settings)


def _judge(monkeypatch, verdict, *, raises: bool = False):
    """Pin the relevance judge's answer without going near an LLM."""
    import app.channels.groups.judge as judge_mod

    async def _fn(**_kw):
        if raises:
            raise RuntimeError("provider exploded")
        return verdict

    monkeypatch.setattr(judge_mod, "judge_relevance", _fn)
