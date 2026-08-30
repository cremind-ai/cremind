"""Telling a mid-flight agent that an awaited result has landed.

The notice interrupts the running turn the same way a message the user typed
does: drained at the TOP of the next step, folded in as a ``role:"user"``
message, and answered with one short sentence before the work resumes. An
arriving result is usually the thing the user is waiting on ("the CI run you
were watching finished"), so it earns the same visibility as the user speaking
— not a line quietly appended to a tool result nobody sees.

It stays deliberately lossy: a turn that ends before another step begins never
shows it, and the turn-end flush injects the result as its own turn instead. The
notice is an optimisation; the flush is the guarantee. That is what lets this
channel be in-memory and best-effort.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("a2a")

from app.agent.reasoning_agent import (  # noqa: E402
    _ACK_REQUEST, _TASK_ACK_REQUEST, ReasoningAgent,
)
from app.events import task_result_inbox  # noqa: E402
from app.utils.task_context import current_task_id_var  # noqa: E402

_RUN = "msg:conv-1:abc"
_CONV = "conv-1"


@pytest.fixture(autouse=True)
def _clean():
    task_result_inbox.clear_all()
    yield
    task_result_inbox.clear_all()


def _agent(*, event_run: bool = False) -> ReasoningAgent:
    """A skeleton with just the attributes the drain path touches."""
    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._turn_messages = []
    agent._tool_result_enabled = True
    agent._tool_result_max_tokens = 4000
    agent._event_run = event_run
    agent.current_step_count = 1
    return agent


def _park(label="CI pipeline", status_word="completed"):
    task_result_inbox.park_if_bound(
        _CONV, {"event_run_id": "r1", "label": label, "status_word": status_word},
    )


def _drain(agent: ReasoningAgent) -> list[dict]:
    token = current_task_id_var.set(_RUN)
    try:
        return agent._drain_task_notices()
    finally:
        current_task_id_var.reset(token)


def test_a_waiting_result_becomes_a_user_message_on_the_next_step():
    task_result_inbox.bind_run(_RUN, _CONV)
    _park()

    out = _drain(_agent())

    assert len(out) == 1
    assert out[0]["role"] == "user"
    content = out[0]["content"]
    assert "[Event task results waiting — 1]" in content
    assert "CI pipeline — completed" in content
    assert "get_event_task_results" in content
    # It must also say that ignoring it is safe, or the agent will derail a
    # perfectly good turn to chase an irrelevant result.
    assert "keep working" in content


def test_the_notice_carries_no_result_text():
    """Only what finished and whether it worked.

    A preview would tempt the model to act on the outcome without ever claiming
    the row — and then the turn-end flush would inject a turn for something it
    had effectively already handled.
    """
    task_result_inbox.bind_run(_RUN, _CONV)
    _park()

    content = _drain(_agent())[0]["content"]

    assert "Result:" not in content
    assert "Awaited:" not in content


def test_the_notice_reads_correctly_when_replayed_in_a_later_turn():
    """It is persisted in the turn's trace, so it must not say "right now".

    Anything present-tense ("you are mid-turn", "this is happening now") becomes
    a lie the moment the transcript is replayed into a later turn's history.
    """
    task_result_inbox.bind_run(_RUN, _CONV)
    _park()

    content = _drain(_agent())[0]["content"]

    assert "finished while this turn was running" in content
    assert "you are currently" not in content.lower()


def test_it_reads_as_this_conversation_history_rather_than_this_agent():
    """Replayed into a later turn it is history, and "a task YOU registered"
    would be read as something the model did in the turn it is reading."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park()

    assert "registered earlier in this conversation" in _drain(_agent())[0]["content"]


def test_several_results_are_listed_together():
    task_result_inbox.bind_run(_RUN, _CONV)
    _park("CI pipeline", "completed")
    _park("error log watcher", "timed out")

    content = _drain(_agent())[0]["content"]

    assert "[Event task results waiting — 2]" in content
    assert "CI pipeline — completed" in content
    assert "error log watcher — timed out" in content


def test_drain_is_once():
    task_result_inbox.bind_run(_RUN, _CONV)
    _park()
    agent = _agent()

    assert len(_drain(agent)) == 1
    assert _drain(agent) == []


def test_nothing_is_drained_when_no_result_is_waiting():
    task_result_inbox.bind_run(_RUN, _CONV)
    assert _drain(_agent()) == []


def test_outside_a_bound_run_it_is_a_no_op():
    _park()   # never parked: no binding
    assert _drain(_agent()) == []


def test_event_runs_never_see_a_notice():
    """A hidden run has no inbox of its own and cannot register tasks."""
    task_result_inbox.bind_run(_RUN, _CONV)
    _park()

    assert _drain(_agent(event_run=True)) == []


def test_tool_results_are_left_completely_alone():
    """The old channel rode the tail of a tool result. Now that the notice is
    its own message, every tool result must be byte-identical to the tool's own
    output — including with something waiting."""
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._append_tool_result("call-1", "the tool's own output")
        agent._append_tool_result("call-2", "# SKILL.md ...", truncate=False)
    finally:
        current_task_id_var.reset(token)

    assert agent._turn_messages[0]["content"] == "the tool's own output"
    assert agent._turn_messages[1]["content"] == "# SKILL.md ..."
    # And nothing was consumed by the append path, so the next step still sees it.
    assert len(_drain(agent)) == 1


# ── the interruption it triggers ────────────────────────────────────────────
#
# Same machinery as a mid-turn user message: a flow break so the UI shows the
# arrival as its own moment, then one tools-withheld call so the user is told
# in a sentence, then the turn carries on.


def test_a_lone_notice_still_earns_a_reply():
    """Structural: the ack call gates on `injected`, which is drained + notices
    — not on a user message having arrived."""
    src = inspect.getsource(ReasoningAgent._loop)
    assert "injected = drained + notices" in src
    assert "if injected:" in src
    assert src.index("_drain_task_notices()") < src.index("if injected:")


def test_a_notice_the_agent_says_nothing_about_leaves_the_flow_unbroken():
    """It has no bubble of its own — unlike a user message, which must sit
    between the work either side of it whether or not the agent replied. A
    break with nothing visible in it would just split the thinking panel for no
    reason the user can see."""
    src = inspect.getsource(ReasoningAgent._loop)
    assert "if drained or ack_chunks:" in src
    # The reply is collected before that decision, so "did it speak?" is known.
    assert src.index("ack_chunks = [") < src.index("if drained or ack_chunks:")


def test_a_lone_notice_uses_the_task_wording():
    src = inspect.getsource(ReasoningAgent._loop)
    assert "request = _ACK_REQUEST if drained else _TASK_ACK_REQUEST" in src


def test_the_task_request_asks_for_news_not_an_answer():
    """Nobody asked a question here, so the reply is a heads-up. It must also
    not report the result: the agent has not read it yet — only the notice."""
    assert "SKIP" in _TASK_ACK_REQUEST
    assert "ONE short sentence" in _TASK_ACK_REQUEST
    assert "not read it yet" in _TASK_ACK_REQUEST
    assert "the work continues immediately afterwards" in _TASK_ACK_REQUEST
    # Distinct from the user-message wording, which answers a question.
    assert _TASK_ACK_REQUEST != _ACK_REQUEST


def test_the_read_tool_is_exempt_from_the_token_clamp():
    """Reading early must not return LESS than letting the turn end would."""
    assert (
        "get_event_task_results", "get_event_task_results",
    ) in ReasoningAgent._UNCLAMPED_LEAVES


def test_reading_the_inbox_discards_the_notices_it_covers():
    """Otherwise the next step interrupts the user about results the agent is
    already holding — the hazard the old per-step suppression guarded."""
    src = inspect.getsource(
        __import__(
            "app.tools.builtin.get_event_task_results", fromlist=["x"],
        ).GetEventTaskResultsTool.run
    )
    read_at = src.index("read_origin_inbox(")
    discard_at = src.index("drain_notices(")
    # After the read returns — a read that raised released its claims, and those
    # rows are still undelivered, so their notices must survive.
    assert read_at < discard_at
