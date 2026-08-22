"""The turn-end flush — the guarantee behind "ignoring a notice is safe".

Three things about its placement are load-bearing, and all three are structural
rather than observable, so they are pinned by reading the source:

* the run binding is released FIRST in the ``finally``, so a result landing
  during teardown delivers itself instead of parking with nobody to read it;
* the flush runs AFTER ``bus.end_run`` and last, so a delivery racing this point
  either parked (and this query sees its row) or took the idle path itself;
* it lives in the ``finally``, so an errored or cancelled turn still reconciles.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("a2a")

import app.agent.stream_runner as sr  # noqa: E402


def _finally_block() -> str:
    src = inspect.getsource(sr.run_agent_to_bus)
    idx = src.rindex("\n    finally:")
    return src[idx:]


def test_the_binding_is_released_before_anything_else_in_teardown():
    block = _finally_block()
    assert "task_result_inbox.unbind_run(run_id)" in block
    assert block.index("unbind_run") < block.index("_running_runs.pop")
    assert block.index("unbind_run") < block.index("bus.end_run")


def test_the_flush_runs_after_end_run_and_is_the_last_thing_the_turn_does():
    """Order is the whole proof.

    Before ``end_run`` this would re-open the stall: a result that parked
    moments earlier would have missed the query, and its row would wait for the
    next turn or the next boot.
    """
    block = _finally_block()
    assert "flush_origin_inbox" in block
    assert block.index("bus.end_run") < block.index("flush_origin_inbox")
    assert block.rstrip().endswith("f\"{conversation_id}\"\n                )")


def test_hidden_event_runs_do_not_flush():
    """An event run is never an origin, so the query would always be empty."""
    assert "if not event_run and task_result_inbox.has_pending(conversation_id)" in (
        _finally_block()
    )


def test_the_pending_marker_gates_the_query_off_the_hot_path():
    """Without the in-memory gate this is a DB round-trip on EVERY turn end."""
    block = _finally_block()
    assert "has_pending(conversation_id)" in block
    assert block.index("has_pending") < block.index("flush_origin_inbox")


def test_a_failed_flush_leaves_the_marker_set_for_the_next_turn():
    """Self-healing: reset only after the flush actually returned."""
    block = _finally_block()
    assert block.index("flush_origin_inbox") < block.index("task_result_inbox.reset")
    assert "except Exception" in block


def test_the_binding_is_installed_first_inside_the_try():
    """A crash before the try must leave NO binding.

    That is the whole reason the fork keys on the binding rather than the bus's
    ``is_active`` flag: ``start_run`` sits outside the try, so a stale active
    flag is reachable — and a stale flag would park every later result for that
    conversation with no turn-end flush left to rescue it.
    """
    src = inspect.getsource(sr.run_agent_to_bus)
    # The try that owns the finally, not the small guarded reads before it.
    main_try = src.rindex("\n    try:", 0, src.rindex("\n    finally:"))
    body = src[main_try + len("\n    try:"):]
    first_statement = next(
        line.strip() for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert first_statement == "task_result_inbox.bind_run(run_id, conversation_id)"
    assert src.index("bus.start_run") < main_try


def test_a_parked_result_raises_no_notification_of_its_own():
    """The origin conversation speaks for it — twice would read as a duplicate."""
    from app.events.event_task_delivery import (
        PARKED, SUPPRESSES_RUN_NOTIFICATION,
    )
    assert PARKED in SUPPRESSES_RUN_NOTIFICATION
