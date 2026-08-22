"""Telling a mid-flight agent that an awaited result has landed.

The notice rides a tool result — the only channel that reaches a running ReAct
loop without touching the prompt-cached prefix or fabricating an assistant
message. It is deliberately lossy: a step with no tool call never sees it, and
the turn-end flush delivers the result as a turn instead. That is what lets the
channel be in-memory and best-effort.
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from app.agent.reasoning_agent import ReasoningAgent  # noqa: E402
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
    """A skeleton with just the attributes the append path touches."""
    agent = ReasoningAgent.__new__(ReasoningAgent)
    agent._turn_messages = []
    agent._tool_result_enabled = True
    agent._tool_result_max_tokens = 4000
    agent._event_run = event_run
    agent.current_step_count = 1
    agent._notice_step = -1
    return agent


def _park(label="CI pipeline", status_word="completed"):
    task_result_inbox.park_if_bound(
        _CONV, {"event_run_id": "r1", "label": label, "status_word": status_word},
    )


def test_a_waiting_result_is_announced_on_the_next_tool_result():
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._append_tool_result("call-1", "the tool's own output")
    finally:
        current_task_id_var.reset(token)

    content = agent._turn_messages[0]["content"]
    assert content.startswith("the tool's own output")
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
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._append_tool_result("call-1", "out")
    finally:
        current_task_id_var.reset(token)

    notice = agent._turn_messages[0]["content"].split("out", 1)[1]
    assert "Result:" not in notice
    assert "Awaited:" not in notice


def test_the_notice_reads_correctly_when_replayed_in_a_later_turn():
    """It is persisted with the tool message, so it must not say "right now".

    Anything present-tense ("you are mid-turn", "this is happening now") becomes
    a lie the moment the transcript is replayed into a later turn's history.
    """
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._append_tool_result("call-1", "out")
    finally:
        current_task_id_var.reset(token)

    content = agent._turn_messages[0]["content"]
    assert "finished while this turn was running" in content
    assert "you are currently" not in content.lower()


def test_it_is_appended_once_per_step_even_with_parallel_tool_calls():
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._append_tool_result("call-1", "first")
        agent._append_tool_result("call-2", "second")
        agent._append_tool_result("call-3", "third")
    finally:
        current_task_id_var.reset(token)

    joined = "".join(m["content"] for m in agent._turn_messages)
    assert joined.count("[Event task results waiting") == 1


def test_it_survives_the_token_clamp():
    """Appended AFTER the clamp — a notice clipped off the tail is no notice."""
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._tool_result_max_tokens = 5
        agent._append_tool_result("call-1", "lorem ipsum " * 400)
    finally:
        current_task_id_var.reset(token)

    assert "get_event_task_results" in agent._turn_messages[0]["content"]


def test_it_rides_an_unclamped_result_too():
    """A long SKILL.md read (truncate=False) is a prime moment for one to land."""
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._append_tool_result("call-1", "# SKILL.md ...", truncate=False)
    finally:
        current_task_id_var.reset(token)

    assert "get_event_task_results" in agent._turn_messages[0]["content"]


def test_nothing_is_appended_when_no_result_is_waiting():
    """The overwhelming majority of tool results must be byte-identical."""
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        agent = _agent()
        agent._append_tool_result("call-1", "plain output")
    finally:
        current_task_id_var.reset(token)

    assert agent._turn_messages[0]["content"] == "plain output"


def test_event_runs_never_see_a_notice():
    """A hidden run has no inbox of its own and cannot read one."""
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent(event_run=True)
        agent._append_tool_result("call-1", "out")
    finally:
        current_task_id_var.reset(token)

    assert agent._turn_messages[0]["content"] == "out"


def test_several_results_are_listed_together():
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park("CI pipeline", "completed")
        _park("error log watcher", "timed out")
        agent = _agent()
        agent._append_tool_result("call-1", "out")
    finally:
        current_task_id_var.reset(token)

    content = agent._turn_messages[0]["content"]
    assert "[Event task results waiting — 2]" in content
    assert "CI pipeline — completed" in content
    assert "error log watcher — timed out" in content


def test_a_step_that_already_read_the_inbox_is_marked_and_says_nothing_more():
    """Guards the in-step ordering hazard.

    Results are emitted in call order, so a sibling tool listed before the read
    would otherwise announce rows that the same step is about to hand over. The
    loop marks the step first; this pins that the mark actually suppresses.
    """
    token = current_task_id_var.set(_RUN)
    try:
        task_result_inbox.bind_run(_RUN, _CONV)
        _park()
        agent = _agent()
        agent._notice_step = agent.current_step_count  # what the loop does
        agent._append_tool_result("call-1", "sibling output")
    finally:
        current_task_id_var.reset(token)

    assert agent._turn_messages[0]["content"] == "sibling output"


def test_the_read_tool_is_exempt_from_the_token_clamp():
    """Reading early must not return LESS than letting the turn end would."""
    assert (
        "get_event_task_results", "get_event_task_results",
    ) in ReasoningAgent._UNCLAMPED_LEAVES
