"""Unit tests for the three hidden Plan-mode tools.

``ask_user_question`` and ``write_plan`` park state in :mod:`app.agent.plan_state`
(keyed by the run id from ``current_task_id_var``) and queue a UI event; the loop
reads that state to end the turn. ``update_todos`` overwrites the todo snapshot
and queues a ``todos`` event without ending the turn.
"""

from __future__ import annotations

import asyncio

from app.agent import plan_state
from app.utils.task_context import current_task_id_var


def _run_with_run_id(coro_factory, run_id="msg:c1:run1"):
    async def _run():
        token = current_task_id_var.set(run_id)
        try:
            return await coro_factory()
        finally:
            current_task_id_var.reset(token)
    try:
        return asyncio.run(_run())
    finally:
        plan_state.clear(run_id)


def _text(result) -> str:
    assert result.content and result.content[0]["type"] == "text"
    return result.content[0]["text"]


# ── ask_user_question ─────────────────────────────────────────────────────

def test_ask_user_question_parks_and_emits():
    from app.tools.builtin.ask_user_question import AskUserQuestionTool
    run_id = "msg:c1:ask"
    tool = AskUserQuestionTool()
    args = {"questions": [
        {"question": "Which module?", "options": [{"label": "auth"}, {"label": "billing"}]},
        {"question": "Scope?"},
    ]}

    async def _run():
        token = current_task_id_var.set(run_id)
        try:
            result = await tool.run(args)
            parked = plan_state.get_questions(run_id)
            emits = plan_state.drain_emit(run_id)
            return result, parked, emits
        finally:
            current_task_id_var.reset(token)

    try:
        result, parked, emits = asyncio.run(_run())
        assert "end your turn" in _text(result).lower()
        assert parked is not None
        qs = parked["questions"]
        assert len(qs) == 2
        assert qs[0]["id"] == "q0" and qs[0]["question"] == "Which module?"
        assert [o["label"] for o in qs[0]["options"]] == ["auth", "billing"]
        assert qs[0]["allow_free_text"] is True  # default
        assert len(emits) == 1 and emits[0]["event"] == "ask_user_question"
    finally:
        plan_state.clear(run_id)


def test_ask_user_question_clamps_to_four():
    from app.tools.builtin.ask_user_question import AskUserQuestionTool
    run_id = "msg:c1:ask4"
    tool = AskUserQuestionTool()
    args = {"questions": [{"question": f"q{i}"} for i in range(7)]}

    async def _run():
        token = current_task_id_var.set(run_id)
        try:
            await tool.run(args)
            return plan_state.get_questions(run_id)
        finally:
            current_task_id_var.reset(token)

    try:
        parked = asyncio.run(_run())
        assert len(parked["questions"]) == 4
    finally:
        plan_state.clear(run_id)


def test_ask_user_question_no_run_id_degrades():
    from app.tools.builtin.ask_user_question import AskUserQuestionTool
    tool = AskUserQuestionTool()
    result = asyncio.run(tool.run({"questions": [{"question": "hi"}]}))
    assert "plain text" in _text(result).lower()


# ── update_todos ──────────────────────────────────────────────────────────

def test_update_todos_overwrites_snapshot_and_emits():
    from app.tools.builtin.update_todos import UpdateTodosTool
    run_id = "msg:c1:todos"
    tool = UpdateTodosTool()

    async def _run():
        token = current_task_id_var.set(run_id)
        try:
            await tool.run({"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
            ]})
            # A second call fully replaces the list.
            r2 = await tool.run({"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "completed"},
                {"content": "c", "status": "pending"},
            ]})
            return r2, plan_state.get_todos(run_id), plan_state.drain_emit(run_id)
        finally:
            current_task_id_var.reset(token)

    try:
        result, todos, emits = asyncio.run(_run())
        assert len(todos) == 3
        assert todos[0]["id"] == "t0"
        assert [t["status"] for t in todos] == ["completed", "completed", "pending"]
        # both calls queued a todos event
        assert len(emits) == 2 and all(e["event"] == "todos" for e in emits)
        assert "keep executing" in _text(result).lower()
    finally:
        plan_state.clear(run_id)


def test_update_todos_bad_status_defaults_pending():
    from app.tools.builtin.update_todos import UpdateTodosTool
    run_id = "msg:c1:badstatus"
    tool = UpdateTodosTool()

    async def _run():
        token = current_task_id_var.set(run_id)
        try:
            await tool.run({"todos": [{"content": "x", "status": "wat"}]})
            return plan_state.get_todos(run_id)
        finally:
            current_task_id_var.reset(token)

    try:
        todos = asyncio.run(_run())
        assert todos[0]["status"] == "pending"
    finally:
        plan_state.clear(run_id)


# ── tool config / schema guards ───────────────────────────────────────────

def test_plan_tools_are_hidden():
    from app.tools.builtin.ask_user_question import TOOL_CONFIG as ask_cfg
    from app.tools.builtin.write_plan import TOOL_CONFIG as plan_cfg
    from app.tools.builtin.update_todos import TOOL_CONFIG as todo_cfg
    for cfg in (ask_cfg, plan_cfg, todo_cfg):
        assert cfg.get("hidden") is True
