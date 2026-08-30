"""Where the group post is made inside a turn, pinned structurally.

In a member's seat the turn's final answer IS its post, so the hook's placement
is the feature's correctness rather than a detail of it:

* it runs AFTER the assistant message is persisted — the post is derived from
  that row, and the row's metadata is what records the outcome, so posting first
  could leave a message in the room that nothing on disk accounts for;
* it runs BEFORE the terminal ``complete`` frame, so a client that reloads the
  room on ``complete`` already sees the post;
* it lives in the ``try``, not the ``finally``: a cancelled turn has nothing to
  say, and the ``finally`` is where the mid-turn flush lives, whose ordering is
  pinned by its own tests;
* the "thinking" indicator is raised with the run binding and dropped in the
  ``finally``, so no path can leave a member lit up forever.

Read from the source because none of it is observable from outside a real run.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("a2a")

import app.agent.stream_runner as sr  # noqa: E402


def _source() -> str:
    return inspect.getsource(sr.run_agent_to_bus)


def _finally_block() -> str:
    src = _source()
    return src[src.rindex("\n    finally:"):]


def _try_block() -> str:
    src = _source()
    return src[: src.rindex("\n    finally:")]


def test_the_post_happens_after_the_assistant_message_is_persisted():
    src = _source()
    assert src.index("assistant_msg = await conversation_storage.add_message") \
        < src.index("on_shadow_turn_complete")


def test_the_post_happens_before_the_terminal_frame():
    src = _source()
    assert src.index("on_shadow_turn_complete") < src.index('"complete", {')


def test_the_post_is_not_in_the_finally():
    """A cancelled or errored turn must not post; the hook is in the try."""
    assert "on_shadow_turn_complete" in _try_block()
    assert "on_shadow_turn_complete" not in _finally_block()


def test_the_post_is_gated_on_the_seat():
    """Every non-group turn must skip it without touching app.groups at all."""
    src = _source()
    idx = src.index("on_shadow_turn_complete")
    guard = src[src.rindex("if ", 0, idx):idx]
    assert "is_group_chat" in guard


def test_the_hook_receives_the_unstripped_text_and_the_break_offsets():
    """The offsets index the raw concatenation, so the stripped ``final_text``
    would cut the segments in the wrong places."""
    src = _source()
    call = src[src.index("await on_shadow_turn_complete("):]
    call = call[: call.index("\n                )")]
    assert 'raw_text="".join(final_text_parts)' in call
    assert "mid_turn_breaks=collected_mid_turn_breaks" in call
    assert "cancelled=cancelled" in call and "errored=errored" in call


def test_thinking_is_published_with_the_run_binding():
    block = _try_block()
    assert block.index("bind_run") < block.index('state="thinking"')


def test_idle_is_published_in_the_finally():
    """However the turn ended — including a crash — the indicator comes down."""
    assert 'state="idle"' in _finally_block()


def test_the_seat_mirror_is_bound_with_the_thinking_indicator():
    """The room is told this member started, and from the same moment it is
    shown what the member is doing — everything published after this point
    belongs to the turn the indicator announces."""
    block = _try_block()
    assert block.index('state="thinking"') < block.index("bind_seat_mirror(conv")
    # Bound before the agent loop, or the first steps reach the room too late.
    assert block.index("bind_seat_mirror(conv") < block.index("async for chunk in")


def test_the_seat_mirror_is_released_in_the_finally():
    """A tap that outlived its turn would narrate the NEXT one under a run the
    room already watched finish."""
    src = _source()
    # Initialised before the turn's try (whose first statement is the run
    # binding), so a turn that dies before the mirror is bound does not take
    # the teardown down with an unbound name.
    assert src.index("seat_mirror = None") < src.index("task_result_inbox.bind_run(")
    assert "unbind_seat_mirror(conversation_id, seat_mirror)" in _finally_block()


def test_the_mirror_is_released_before_idle_is_announced():
    """``publish_agent_status`` waits on a room lookup. With the tap still
    attached across that await, anything published in the gap — a cwd write from
    the files API, any racing publisher — is mirrored AFTER "idle" and puts back
    the live card the status had just cleared."""
    block = _finally_block()
    assert block.index("unbind_seat_mirror(") < block.index('state="idle"')


def test_the_existing_finally_ordering_survives():
    """The group hooks must not disturb the mid-turn flush contract."""
    block = _finally_block()
    assert block.index("unbind_run") < block.index("flush_user_inbox")
    assert block.index("bus.end_run") < block.index("flush_user_inbox")
    assert block.index("flush_user_inbox") < block.index("flush_origin_inbox")


def test_the_seat_compacts_itself_rather_than_suggesting_it():
    """A seat is hidden from the sidebar and the room listens on the group bus, so
    its "compact now" suggestion reaches nobody — leaving it to run forever on the
    deterministic floor, which drops the oldest turns instead of summarising them."""
    src = _source()
    call = src[src.index("compaction.after_turn_compaction("):]
    call = call[: call.index("\n                )")]
    assert "force_auto=is_group_chat" in call


def test_a_platform_group_compacts_itself_too():
    """Same reasoning, different room: a channel group's conversation is visible
    in the sidebar but nobody is sitting in front of it — the people talking are
    on Zalo — so a suggestion waits for a click that never comes."""
    src = _source()
    call = src[src.index("compaction.after_turn_compaction("):]
    call = call[: call.index("\n                )")]
    assert "is_channel_group" in call


# ── the platform group's outcome stamp ────────────────────────────────────


def test_the_group_outcome_is_stamped_before_the_terminal_frame():
    """Written inline rather than by the channel forwarder, which runs
    concurrently with the group's next message: a late stamp means one turn
    replays a "[silent]" row and the next drops it, deleting from the middle of
    the model's history and costing the cached prefix."""
    src = _source()
    assert src.index('"channel_group": {') < src.index('"complete", {')


def test_the_group_outcome_is_stamped_after_the_message_exists():
    """The stamp patches the assistant row, so the row has to be there."""
    src = _source()
    assert src.index("assistant_msg = await conversation_storage.add_message") \
        < src.index('"channel_group": {')


def test_the_stamp_judges_silence_the_same_way_the_post_does():
    """``strip_silent_lines`` is shared with the forwarder on purpose: two
    implementations that disagreed would mean a post the history says never
    happened."""
    src = _source()
    assert "strip_silent_lines" in src


def test_the_origin_branch_precedes_the_channel_lookup():
    """A seat lives on the profile's ``main`` channel, so resolving the channel
    first would describe the room as the Web UI."""
    src = inspect.getsource(sr._resolve_message_origin)
    assert src.index('conv.get("kind") == "group_chat"') < src.index("get_channel")
