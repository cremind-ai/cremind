"""A turn that PULLS a result must inherit the chain it is continuing.

The runaway guard counts how many times a flow has waited, and that count rides
the trigger event. A turn that read its result with ``get_event_task_results``
carries no trigger event at all — so without carrying the depth across, a
wait → read → register → wait loop restarts the counter on every hop and never
hits the cap. All three places that check the cap must agree, including the
skill-subscribe path, which does its own comparison.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("a2a")

from app.agent.reasoning_agent import ReasoningAgent  # noqa: E402
from app.events import task_result_inbox  # noqa: E402
from app.events.task_policy import MAX_TASK_CHAIN_DEPTH  # noqa: E402
from app.utils.task_context import current_task_id_var  # noqa: E402

_RUN = "msg:conv-1:abc"


@pytest.fixture(autouse=True)
def _clean():
    task_result_inbox.clear_all()
    yield
    task_result_inbox.clear_all()


def _agent(*, triggered=True, depth=0) -> ReasoningAgent:
    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._event_run = False
    agent._triggered_by_event = triggered
    agent._task_chain_depth = depth
    return agent


def test_a_pulled_result_still_counts_toward_the_chain():
    agent = _agent(depth=0)
    token = current_task_id_var.set(_RUN)
    try:
        assert agent._effective_chain_depth() == 0
        task_result_inbox.note_consumed_depth(_RUN, 6)
        assert agent._effective_chain_depth() == 7, "the read hop counts"
    finally:
        current_task_id_var.reset(token)


def test_the_deeper_of_the_two_wins():
    """A trigger-borne depth and a read depth can both be present."""
    agent = _agent(depth=9)
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.note_consumed_depth(_RUN, 2)
        assert agent._effective_chain_depth() == 9
    finally:
        current_task_id_var.reset(token)


def test_reading_a_deep_chain_trips_the_registration_cap():
    agent = _agent(depth=0)
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.note_consumed_depth(_RUN, MAX_TASK_CHAIN_DEPTH)
        refusal = agent._registration_refusal()
    finally:
        current_task_id_var.reset(token)
    assert "which is the limit" in refusal
    assert str(MAX_TASK_CHAIN_DEPTH + 1) in refusal


def test_outside_a_run_it_falls_back_to_the_trigger_depth():
    agent = _agent(depth=3)
    assert agent._effective_chain_depth() == 3


def test_every_cap_check_uses_the_effective_depth():
    """Three call sites, and the skill path is the one that gets forgotten.

    ``_handle_skill_call`` compares the depth itself rather than going through
    ``_is_event_blocked_leaf``, so a fix applied only to the latter leaves skill
    tasks chaining without limit.
    """
    for fn in (
        ReasoningAgent._is_event_blocked_leaf,
        ReasoningAgent._registration_refusal,
        ReasoningAgent._handle_skill_call,
    ):
        src = inspect.getsource(fn)
        if "MAX_TASK_CHAIN_DEPTH" not in src:
            continue
        assert "_effective_chain_depth()" in src, fn.__name__
        assert "self._task_chain_depth >= MAX_TASK_CHAIN_DEPTH" not in src, fn.__name__


def test_a_user_typed_turn_keeps_full_registration_rights():
    """The cap is about UNATTENDED ping-pong; a present human resets that.

    ``_is_event_blocked_leaf`` returns early for a turn the user started, so
    reading a result there never costs them the ability to set up an automation.
    """
    src = inspect.getsource(ReasoningAgent._is_event_blocked_leaf)
    guard = src.index("if not self._triggered_by_event")
    assert guard < src.index("_effective_chain_depth")
