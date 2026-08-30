"""The routing state for user messages that arrive mid-turn.

The contract that matters is exactly-once: a parked message is either handed to
the running turn (and committed when that turn's trace persists) or taken by the
turn-end flush — never both, and never neither. These tests pin the state
machine that guarantees it, including the two things that make the handoff sound:
parking is refused the instant the binding is gone, and a refusal is never a drop.
"""

from __future__ import annotations

import inspect

import pytest

from app.events import task_result_inbox

_RUN = "msg:conv-1:abc"
_CONV = "conv-1"


@pytest.fixture(autouse=True)
def _clean():
    task_result_inbox.clear_all()
    yield
    task_result_inbox.clear_all()


def _payload(n: int) -> dict:
    return {"message_id": f"m{n}", "text": f"msg {n}", "agent_text": f"msg {n}"}


def test_parking_needs_a_live_turn():
    assert task_result_inbox.park_user_message_if_bound(_CONV, _payload(1)) is None

    task_result_inbox.bind_run(_RUN, _CONV)
    assert task_result_inbox.park_user_message_if_bound(_CONV, _payload(1)) == _RUN


def test_unbinding_closes_the_door_immediately():
    """The turn ended: the caller must run the message itself, not park it."""
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.unbind_run(_RUN)

    assert task_result_inbox.park_user_message_if_bound(_CONV, _payload(1)) is None


def test_park_is_one_synchronous_function():
    """No ``await`` may separate the liveness check from the park.

    An await there would let a turn end between the two and strand the message
    with nobody left to read it. Structural, because the property is invisible
    at runtime until it breaks.
    """
    src = inspect.getsource(task_result_inbox.park_user_message_if_bound)
    assert "await" not in src
    assert not inspect.iscoroutinefunction(
        task_result_inbox.park_user_message_if_bound
    )


def test_drain_hands_over_once_and_keeps_them_uncommitted():
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(1))
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(2))

    drained = task_result_inbox.drain_user_messages(_RUN)
    assert [p["message_id"] for p in drained] == ["m1", "m2"]
    assert task_result_inbox.drain_user_messages(_RUN) == []      # drain-once
    # Still on the hook until the turn's trace persists.
    assert task_result_inbox.has_unconsumed_user_messages(_CONV)


def test_commit_clears_them():
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(1))
    task_result_inbox.drain_user_messages(_RUN)

    committed = task_result_inbox.commit_user_messages(_CONV)
    assert [p["message_id"] for p in committed] == ["m1"]
    assert not task_result_inbox.has_unconsumed_user_messages(_CONV)
    assert task_result_inbox.take_unconsumed_user_messages(_CONV) == []


def test_take_returns_drained_before_parked():
    """A turn that died mid-flight owes answers in the order they were sent."""
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(1))
    task_result_inbox.drain_user_messages(_RUN)                    # m1 in flight
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(2))  # m2 waiting

    taken = task_result_inbox.take_unconsumed_user_messages(_CONV)
    assert [p["message_id"] for p in taken] == ["m1", "m2"]
    assert not task_result_inbox.has_unconsumed_user_messages(_CONV)


def test_drained_messages_survive_unbind():
    """The flush runs after unbind_run, so it must still find them."""
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(1))
    task_result_inbox.drain_user_messages(_RUN)

    task_result_inbox.unbind_run(_RUN)

    assert task_result_inbox.has_unconsumed_user_messages(_CONV)
    assert len(task_result_inbox.take_unconsumed_user_messages(_CONV)) == 1


def test_at_capacity_a_park_is_refused_not_dropped():
    task_result_inbox.bind_run(_RUN, _CONV)
    for i in range(task_result_inbox._MAX_USER_PARKED):
        assert task_result_inbox.park_user_message_if_bound(_CONV, _payload(i))

    # Refused → the caller runs it as its own turn. The message is never lost.
    assert task_result_inbox.park_user_message_if_bound(_CONV, _payload(999)) is None
    kept = task_result_inbox.take_unconsumed_user_messages(_CONV)
    assert len(kept) == task_result_inbox._MAX_USER_PARKED


def test_capacity_counts_drained_messages_too():
    task_result_inbox.bind_run(_RUN, _CONV)
    for i in range(task_result_inbox._MAX_USER_PARKED):
        task_result_inbox.park_user_message_if_bound(_CONV, _payload(i))
    task_result_inbox.drain_user_messages(_RUN)  # moves them, doesn't free space

    assert task_result_inbox.park_user_message_if_bound(_CONV, _payload(999)) is None


def test_discard_forgets_a_deleted_conversation():
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(1))
    task_result_inbox.drain_user_messages(_RUN)
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(2))

    task_result_inbox.discard(_CONV)

    assert not task_result_inbox.has_unconsumed_user_messages(_CONV)
    assert task_result_inbox.bound_run_for(_CONV) is None


def test_bound_run_for_reports_the_live_turn():
    assert task_result_inbox.bound_run_for(_CONV) is None
    task_result_inbox.bind_run(_RUN, _CONV)
    assert task_result_inbox.bound_run_for(_CONV) == _RUN
    task_result_inbox.unbind_run(_RUN)
    assert task_result_inbox.bound_run_for(_CONV) is None


def test_user_messages_and_task_notices_are_separate_channels():
    task_result_inbox.bind_run(_RUN, _CONV)
    task_result_inbox.park_if_bound(_CONV, {"label": "CI", "status_word": "completed"})
    task_result_inbox.park_user_message_if_bound(_CONV, _payload(1))

    # Draining one must not consume the other.
    assert len(task_result_inbox.drain_notices(_RUN)) == 1
    assert len(task_result_inbox.drain_user_messages(_RUN)) == 1
