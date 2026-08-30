"""The turn-end flush for mid-turn user messages — the "always answered" guarantee.

Injection is an optimisation: a message can arrive during the final step, or the
turn can be cancelled before its trace persists. This flush is what makes those
cases produce an answer anyway. Its placement carries the correctness, and the
placement is structural, so it is pinned by reading the source:

* it lives in the ``finally``, so a cancelled or errored turn still reconciles;
* it runs AFTER ``bus.end_run`` — before it, the follow-up run would try to start
  while the bus still considers the finished run active;
* it runs BEFORE the task-result flush, so the person waiting for a reply is
  served ahead of background bookkeeping;
* it is NOT gated on ``event_run`` (the task-result flush is): a reply to a
  running event run is exactly what this exists for;
* the commit that marks messages consumed is gated on the persisted trace, which
  is what stops a message being both injected-and-committed AND re-delivered.
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


def test_the_user_flush_lives_in_the_finally():
    assert "flush_user_inbox" in _finally_block()


def test_it_runs_after_end_run():
    block = _finally_block()
    assert block.index("bus.end_run") < block.index("flush_user_inbox")


def test_it_runs_before_the_task_result_flush():
    """A person waiting on a reply comes before background reconciliation."""
    block = _finally_block()
    assert block.index("flush_user_inbox") < block.index("flush_origin_inbox")


def test_the_binding_is_still_released_first():
    block = _finally_block()
    assert block.index("unbind_run") < block.index("flush_user_inbox")


def test_the_user_flush_is_not_gated_on_event_run():
    """Unlike the task-result flush: an event-run reply must be answered too."""
    block = _finally_block()
    gate = block[block.index("has_unconsumed_user_messages"):]
    gate = gate[: gate.index("flush_user_inbox")]
    assert "event_run" not in gate
    # The task-result flush right below it still IS gated, unchanged.
    assert "if not event_run and task_result_inbox.has_pending" in block


def test_the_commit_requires_a_persisted_trace():
    """Without the trace the injection is not durable, so it must NOT commit —
    that is what routes a cancelled turn's messages to the flush instead."""
    src = _source()
    idx = src.index("commit_user_messages")
    guard = src[src.rindex("if ", 0, idx):idx]
    assert "collected_llm_messages" in guard
    assert "assistant_msg_id" in guard


def test_the_commit_happens_after_the_assistant_message_is_persisted():
    src = _source()
    assert src.index("assistant_msg = await conversation_storage.add_message") \
        < src.index("commit_user_messages")


def test_complete_advertises_a_queued_followup():
    """Clients hold their streaming state across the enqueue gap on this flag."""
    src = _source()
    complete = src[src.index('"complete", {'):]
    complete = complete[: complete.index("})")]
    assert "followup_queued" in complete
