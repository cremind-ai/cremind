"""Plan-mode phase computation + persisted-metadata assembly in stream_runner."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a")

import app.agent.stream_runner as sr  # noqa: E402
from app.agent import plan_state  # noqa: E402


class _FakeStorage:
    def __init__(self, messages):
        self._messages = messages

    async def get_messages_after(self, conversation_id, after, limit=50, newest_first=False):
        return list(self._messages)


def _phase(messages, plan_action=None):
    return asyncio.run(sr._compute_plan_phase(_FakeStorage(messages), "c1", plan_action))


# ── _compute_plan_phase ────────────────────────────────────────────────────

def test_accept_action_routes_to_execute():
    assert _phase([], plan_action="accept") == "execute"


def test_fresh_conversation_is_planning():
    assert _phase([]) == "planning"


def test_answered_questions_is_planning():
    msgs = [{"role": "agent", "metadata": {"plan_mode": {"stage": "awaiting_answers", "questions": []}}}]
    assert _phase(msgs) == "planning"


def test_awaiting_approval_is_planning():
    msgs = [{"role": "agent", "metadata": {"plan_mode": {"stage": "awaiting_approval", "plan": {}}}}]
    assert _phase(msgs) == "planning"


def test_executing_incomplete_resumes_execute():
    msgs = [{"role": "agent", "metadata": {"plan_mode": {
        "stage": "executing",
        "todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}],
    }}}]
    assert _phase(msgs) == "execute"


def test_executing_all_complete_is_planning():
    msgs = [{"role": "agent", "metadata": {"plan_mode": {
        "stage": "executing",
        "todos": [{"content": "a", "status": "completed"}],
    }}}]
    assert _phase(msgs) == "planning"


def test_latest_plan_message_decides():
    # Newest plan-bearing agent message wins (list is chronological).
    msgs = [
        {"role": "agent", "metadata": {"plan_mode": {"stage": "executing",
            "todos": [{"content": "a", "status": "pending"}]}}},
        {"role": "user", "content": "done, thanks"},
        {"role": "agent", "metadata": {"plan_mode": {"stage": "completed",
            "todos": [{"content": "a", "status": "completed"}]}}},
    ]
    assert _phase(msgs) == "planning"


# ── _plan_metadata_for_persist ─────────────────────────────────────────────

def test_metadata_questions_stage():
    run_id = "msg:c1:mq"
    plan_state.mark_questions(run_id, {"questions": [{"id": "q0", "question": "?"}]})
    try:
        meta = sr._plan_metadata_for_persist(run_id, "planning", cancelled=False)
        assert meta["stage"] == "awaiting_answers"
        assert meta["questions"][0]["id"] == "q0"
    finally:
        plan_state.clear(run_id)


def test_metadata_plan_stage_strips_markdown():
    run_id = "msg:c1:mp"
    plan_state.mark_plan(run_id, {
        "path": "/p/plan.md", "filename": "plan.md", "title": "T",
        "markdown": "# big plan", "status": "awaiting_approval",
    })
    try:
        meta = sr._plan_metadata_for_persist(run_id, "planning", cancelled=False)
        assert meta["stage"] == "awaiting_approval"
        assert meta["plan"] == {"path": "/p/plan.md", "filename": "plan.md", "title": "T"}
        assert "markdown" not in meta["plan"]  # lives in message content, not metadata
    finally:
        plan_state.clear(run_id)


def test_metadata_todos_executing_vs_completed():
    run_id = "msg:c1:mt"
    plan_state.set_todos(run_id, [
        {"id": "t0", "content": "a", "status": "completed"},
        {"id": "t1", "content": "b", "status": "pending"},
    ])
    try:
        meta = sr._plan_metadata_for_persist(run_id, "execute", cancelled=False)
        assert meta["stage"] == "executing"
        assert len(meta["todos"]) == 2
    finally:
        plan_state.clear(run_id)

    run_id2 = "msg:c1:mt2"
    plan_state.set_todos(run_id2, [{"id": "t0", "content": "a", "status": "completed"}])
    try:
        meta = sr._plan_metadata_for_persist(run_id2, "execute", cancelled=False)
        assert meta["stage"] == "completed"
    finally:
        plan_state.clear(run_id2)


def test_metadata_cancelled_execution_keeps_todos_but_not_completed():
    run_id = "msg:c1:mc"
    plan_state.set_todos(run_id, [{"id": "t0", "content": "a", "status": "completed"}])
    try:
        # Even if all todos completed, a cancelled turn is recorded as executing
        # so a resume re-enters the execution phase.
        meta = sr._plan_metadata_for_persist(run_id, "execute", cancelled=True)
        assert meta["stage"] == "executing"
        assert meta["todos"][0]["id"] == "t0"
    finally:
        plan_state.clear(run_id)


def test_metadata_cancelled_skips_parked_questions():
    run_id = "msg:c1:mcq"
    plan_state.mark_questions(run_id, {"questions": [{"id": "q0", "question": "?"}]})
    try:
        # A cancelled planning turn must not persist a half-parked question.
        meta = sr._plan_metadata_for_persist(run_id, "planning", cancelled=True)
        assert meta is None
    finally:
        plan_state.clear(run_id)


def test_metadata_event_run_persists_todos_with_no_phase():
    # Event runs expose update_todos but have plan_phase=None. The todos snapshot
    # must still persist (as the {stage, todos} branch) so the run's live panel
    # restores on reload — questions/plan tools are never exposed there.
    run_id = "msg:c1:evt"
    plan_state.set_todos(run_id, [
        {"id": "t0", "content": "fetch page", "status": "completed"},
        {"id": "t1", "content": "email results", "status": "in_progress"},
    ])
    try:
        meta = sr._plan_metadata_for_persist(run_id, None, cancelled=False)
        assert meta["stage"] == "executing"
        assert len(meta["todos"]) == 2
        assert "questions" not in meta and "plan" not in meta
    finally:
        plan_state.clear(run_id)
