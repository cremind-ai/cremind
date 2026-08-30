"""Getting the agent's answer back into the room — and knowing when not to.

The reply machinery was built for one sender and is now keyed on a
:class:`~app.channels.reply_target.ReplyTarget`, so the DM path has to be
provably unchanged while the group path diverges in three ways: it addresses a
chat id, it never posts an error, and a ``[silent]`` answer sends nothing at all.

"Answer with steps" is NOT one of the differences — the channel's setting
governs a room as well — but silence and steps interact, so the turn that says
nothing must post no steps either.
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels.reply_target import (
    coerce_target, group_key, group_target, sender_target,
)
from tests.channels.groups.conftest import RoomAdapter, make_adapter


class _RealForwarderAdapter(RoomAdapter):
    """The shared harness stubs ``_forward_reply``; this file is about it.

    Everything else — the send recording, the fake storage — is reused as-is.
    """

    _forward_reply = RoomAdapter.__mro__[1]._forward_reply


def _adapter(**kw):
    adapter = make_adapter(**kw)
    adapter.__class__ = _RealForwarderAdapter
    return adapter


def _group_row(**overrides):
    row = {"id": "g-1", "platform_chat_id": "-1001", "status": "approved",
           "channel_id": "ch-1"}
    row.update(overrides)
    return row


# ── the target ────────────────────────────────────────────────────────────


def test_a_dm_target_keeps_the_bare_sender_id_as_its_key():
    """Everything about the 1:1 path — the in-flight table, the tests that index
    it, ``forget_sender`` — is keyed on the sender id, and stays that way."""
    target = sender_target("u1")
    assert target.key == "u1"
    assert target.address == "u1"
    assert target.is_group is False


def test_a_group_target_is_namespaced_by_its_group_id():
    target = group_target(_group_row())
    assert target.key == "cg:g-1"
    assert target.address == "-1001"
    assert target.is_group is True


def test_a_bare_string_still_means_a_sender():
    """The 1:1 call sites pass a sender id and should not have to know rooms
    exist."""
    assert coerce_target("u1") == sender_target("u1")
    assert coerce_target(sender_target("u1")).key == "u1"


# ── the reply path ────────────────────────────────────────────────────────


def _run_forward(adapter, frames, *, target, response_mode="normal"):
    """Drive ``_forward_reply`` against a scripted stream of bus frames."""
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
        asyncio.run(adapter._forward_reply("conv-1", target))
    finally:
        base_mod.get_event_stream_bus = original


def _text(token, seq: int = 2):
    # Distinct seqs matter: the forwarder dedupes on them, so two frames sharing
    # one would silently swallow the second.
    return {"seq": seq, "type": "text", "data": {"token": token}}


def _complete(assistant_id="a-1"):
    return {"seq": 9, "type": "complete", "data": {"assistant_id": assistant_id}}


def test_a_group_answer_goes_to_the_chat_not_to_a_sender():
    adapter = _adapter()
    _run_forward(
        adapter, [_text("all green"), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [("-1001", "all green")]
    assert adapter.sent == []


def test_a_dm_answer_still_goes_through_the_sender_path():
    adapter = _adapter()
    _run_forward(
        adapter, [_text("hello"), _complete()], target=sender_target("u1"),
    )
    assert adapter.sent == [("u1", "hello")]
    assert adapter.chat_sends == []


def _thinking(thought="checking", action="shell", action_input="kubectl get pods",
              seq: int = 1):
    return {"seq": seq, "type": "thinking",
            "data": {"Thought": thought, "Action": action,
                     "Action_Input": action_input}}


def test_a_room_receives_the_steps_when_the_channel_asks_for_them():
    """The reported bug: "Answer with steps" was set on the channel and a room
    answered in one condensed message anyway — the target used to veto detail
    outright, so the setting could not reach a group at all."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [_thinking(), _text("all green", seq=2), _complete()],
        target=group_target(_group_row()),
        response_mode="detail",
    )
    step, answer = adapter.chat_sends
    assert step[0] == "-1001"
    assert "kubectl get pods" in step[1]
    assert "checking" in step[1]
    assert answer[1].endswith("all green")


def test_a_room_still_gets_only_the_answer_on_final_answer_only():
    """The other half of the same setting: nothing about "normal" moved."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [_thinking(), _text("all green", seq=2), _complete()],
        target=group_target(_group_row()),
        response_mode="normal",
    )
    assert adapter.chat_sends == [("-1001", "all green")]


def test_a_turn_that_stays_silent_posts_no_steps_either():
    """Steps in a room are only safe because the turn that decides the message
    was not for it says nothing at all — including about its reasoning. Its one
    step is still pending when ``complete`` arrives, so it can be held back."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [_thinking(thought="not addressed to me"), _text("[silent]", seq=2),
         _complete()],
        target=group_target(_group_row()),
        response_mode="detail",
    )
    assert adapter.chat_sends == []


def test_an_interrupted_turn_shows_its_steps_and_its_interim_reply():
    """Detail mode and the mid-turn reply are independent features that share the
    ``flow_break`` frame; a room gets both."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [
            _thinking(action_input="apt install openclaw"),
            _text("Not yet — still installing.", seq=2),
            _flow_break(),
            _text("Done.", seq=4),
            _complete(),
        ],
        target=group_target(_group_row()),
        response_mode="detail",
    )
    bodies = [body for _chat, body in adapter.chat_sends]
    assert any("apt install openclaw" in b for b in bodies)
    assert "Not yet — still installing." in bodies
    assert bodies[-1].endswith("Done.")


def test_the_trigger_header_goes_to_the_chat_not_to_a_sender():
    """It is the one send in ``absorb`` that used to bypass ``_send_reply``. A
    room's address is a platform CHAT id — on Telegram a negative number that
    addresses no user — so the DM path would have thrown it into the void."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [
            {"seq": 1, "type": "event_trigger_message",
             "data": {"content": "Trigger: schedule"}},
            _text("done", seq=2),
            _complete(),
        ],
        target=group_target(_group_row()),
        response_mode="detail",
    )
    assert adapter.sent == []
    assert adapter.chat_sends[0] == ("-1001", "Trigger: schedule")


def test_a_silent_answer_posts_nothing_at_all():
    adapter = _adapter()
    _run_forward(
        adapter, [_text("[silent]"), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == []


def test_an_interrupted_turn_posts_what_it_meant_to_say():
    """A turn cut mid-flight speaks twice — an acknowledgement, then, once it has
    looked, the sentinel. The forwarder sees only the concatenation, so it has to
    drop the sentinel LINE rather than test the whole string."""
    adapter = _adapter()
    _run_forward(
        adapter, [_text("Got it, checking\n\n[silent]"), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [("-1001", "Got it, checking")]


# ── answering an interruption while the work runs ──────────────────────────


def _flow_break(seq: int = 3):
    return {"seq": seq, "type": "flow_break", "data": {"message_ids": ["m1"]}}


def test_a_reply_to_an_interruption_is_sent_before_the_turn_ends():
    """The reported bug. Asked "have you finished installing?" during a long
    install, the agent's "not yet" was buffered with everything else and arrived
    once the install was over — the one moment it was worth nothing."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [
            _text("Not yet — still installing.", seq=2),
            _flow_break(),
            _text("Done, all installed.", seq=4),
            _complete(),
        ],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [
        ("-1001", "Not yet — still installing."),
        ("-1001", "Done, all installed."),
    ]


def test_the_dm_path_answers_interruptions_the_same_way():
    adapter = _adapter()
    _run_forward(
        adapter,
        [_text("Not yet.", seq=2), _flow_break(), _text("Done.", seq=4), _complete()],
        target=sender_target("u1"),
    )
    assert adapter.sent == [("u1", "Not yet."), ("u1", "Done.")]


def test_a_flow_break_does_not_end_the_run():
    """It is a pause, not a terminal event: the forwarder has to keep reading or
    the answer that follows it never reaches the platform at all."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [_flow_break(seq=1), _text("the answer", seq=2), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [("-1001", "the answer")]


def test_an_interim_segment_is_never_repeated_at_the_end():
    """The buffer is what gets flushed, so failing to clear it would say the
    interim reply again glued to the final answer."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [_text("Not yet.", seq=2), _flow_break(), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [("-1001", "Not yet.")]


def test_a_silent_interim_is_spent_not_carried():
    """It declined to answer that one. The sentinel must not ride along into
    whatever it says next."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [
            _text("[silent]", seq=2),
            _flow_break(),
            _text("Done.", seq=4),
            _complete(),
        ],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [("-1001", "Done.")]


def test_the_final_answer_fallback_does_not_repeat_what_already_went_out():
    """The fallback exists for a turn whose text never streamed. After an
    interim flush it holds text the room has already read."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [
            {"seq": 1, "type": "thinking",
             "data": {"Action": "Final Answer", "Action_Input": "Not yet."}},
            _text("Not yet.", seq=2),
            _flow_break(),
            _complete(),
        ],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == [("-1001", "Not yet.")]


def test_an_error_is_logged_not_posted_into_the_room():
    """Everyone in the group would read an apology that means nothing to them;
    the operator needs the log line instead."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [{"seq": 1, "type": "error", "data": {"message": "boom"}}],
        target=group_target(_group_row()),
    )
    assert adapter.chat_sends == []


def test_an_error_still_reaches_a_dm():
    adapter = _adapter()
    _run_forward(
        adapter,
        [{"seq": 1, "type": "error", "data": {"message": "boom"}}],
        target=sender_target("u1"),
    )
    assert adapter.sent and "boom" in adapter.sent[0][1]


@pytest.mark.parametrize("token", ["all green", "[silent]"])
def test_the_forwarder_does_not_stamp_the_turns_outcome(token):
    """The stamp is written by the stream runner, inline, before ``complete``.

    It used to be written here, and that was a race with real consequences: this
    forwarder runs concurrently with whatever the group says next, so a stamp
    landing late meant one turn replayed a "[silent]" row that the next one
    dropped. A deletion in the middle of the model's history invalidates the
    cached prefix from that point on, every time it happens.
    """
    adapter = _adapter()
    _run_forward(
        adapter, [_text(token), _complete("a-7")],
        target=group_target(_group_row()),
    )
    assert adapter.storage.metadata_patches == []


def test_a_sent_group_answer_counts_towards_the_rate_cap():
    adapter = _adapter()
    _run_forward(
        adapter, [_text("all green"), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.groups.agent_posts_last_minute("g-1") == 1


def test_a_silent_turn_spends_no_rate_budget():
    """A room that mostly stays quiet must not brake itself."""
    adapter = _adapter()
    _run_forward(
        adapter, [_text("[silent]"), _complete()],
        target=group_target(_group_row()),
    )
    assert adapter.groups.agent_posts_last_minute("g-1") == 0


def test_reasoning_steps_do_not_spend_the_rate_budget():
    """A step is not a conversational turn. The cap is 20 posts a minute and is
    checked when the NEXT message arrives, so counting steps would mean a long
    detailed turn silenced the room for a minute — turning "Answer with steps"
    into a switch that shuts the agent up."""
    adapter = _adapter()
    _run_forward(
        adapter,
        [
            _thinking(action_input="one", seq=1),
            _thinking(action_input="two", seq=2),
            _thinking(action_input="three", seq=3),
            _text("all green", seq=4),
            _complete(),
        ],
        target=group_target(_group_row()),
        response_mode="detail",
    )
    assert len(adapter.chat_sends) > 1
    assert adapter.groups.agent_posts_last_minute("g-1") == 1


# ── covering runs nobody pointed a forwarder at ───────────────────────────


def test_a_run_finishing_in_a_group_conversation_is_carried_out(group_storage):
    """A mid-turn flush, a task result or a schedule can finish a turn in a
    group's conversation with no forwarder bound to it — and then nothing would
    carry the answer back to the room."""
    adapter = make_adapter()

    async def _run():
        group = await group_storage.create_group(
            channel_id="ch-1", profile="admin", platform_chat_id="-1001",
        )
        await group_storage.update_group(
            group["id"], status="approved", conversation_id="conv-9",
        )
        await adapter.forward_external_run("conv-9")
        # Asserted INSIDE the loop: the harness's forwarder returns at once, and
        # its done-callback clears the entry as soon as the loop next runs.
        assert group_key(group["id"]) in adapter._inflight
        await adapter.stop()

    asyncio.run(_run())


def test_a_blocked_group_is_not_carried(group_storage):
    """A run finishing in a group the operator has since refused must not post
    into it."""
    adapter = make_adapter()

    async def _run():
        group = await group_storage.create_group(
            channel_id="ch-1", profile="admin", platform_chat_id="-1001",
        )
        await group_storage.update_group(
            group["id"], status="blocked", conversation_id="conv-9",
        )
        await adapter.forward_external_run("conv-9")

    asyncio.run(_run())
    assert adapter._inflight == {}


def test_forgetting_a_group_drops_its_forwarder(group_storage):
    adapter = make_adapter()

    async def _run():
        group = await group_storage.create_group(
            channel_id="ch-1", profile="admin", platform_chat_id="-1001",
        )
        await group_storage.update_group(
            group["id"], status="approved", conversation_id="conv-9",
        )
        await adapter.forward_external_run("conv-9")
        adapter.groups.note_agent_post(group["id"])
        adapter.forget_group(group["id"], "-1001")
        assert group_key(group["id"]) not in adapter._inflight
        assert adapter.groups.agent_posts_last_minute(group["id"]) == 0

    asyncio.run(_run())


# ── the feature gate ──────────────────────────────────────────────────────


def test_groups_are_off_for_a_notification_channel():
    """A notification channel pushes automation output outward and holds no
    conversations, so there is nothing for it to say in a group."""
    adapter = make_adapter()
    adapter.channel["mode"] = "notification"
    assert adapter.groups_enabled() is False


def test_groups_are_off_when_the_config_switch_is():
    assert make_adapter(enabled=False).groups_enabled() is False
    assert make_adapter(enabled=True).groups_enabled() is True
