"""In-memory plan-mode registry, keyed by the stream ``run_id``.

Plan mode's three hidden tools (``ask_user_question``, ``write_plan``,
``update_todos``) cannot reach the conversation stream bus — a built-in tool
only receives ``_context_id``/``_profile``, never the ``conversation_id`` the bus
is keyed by — and cannot reach the assistant message metadata. So instead they
record their effect here, keyed by the stream ``run_id`` read from
``current_task_id_var``:

* The reasoning loop drains queued UI ``emit`` items each step, yielding them as
  ``PLAN_EVENT`` chunks that ``stream_runner`` translates into bus events, and
  checks the parked question/plan to end the turn.
* ``stream_runner`` reads the final state at persist time and stamps it onto
  ``messages.message_metadata`` so a reload can restore the pending form / plan
  approval / todo panel.

Purely transient: entries survive only from a tool call to the end of the same
turn. Durable state lives in ``message_metadata`` + the plan ``.md`` file on
disk. Cleared at run start and unconditionally in ``run_agent_to_bus``'s
``finally`` block.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class _RunPlanState:
    __slots__ = ("questions", "plan", "todos", "emits")

    def __init__(self) -> None:
        self.questions: Optional[Dict[str, Any]] = None
        self.plan: Optional[Dict[str, Any]] = None
        self.todos: Optional[List[Dict[str, Any]]] = None
        self.emits: List[Dict[str, Any]] = []


# run_id → per-run plan state. Presence of ``questions``/``plan`` means "this
# turn parked pending".
_states: Dict[str, _RunPlanState] = {}


def _get(run_id: str) -> _RunPlanState:
    st = _states.get(run_id)
    if st is None:
        st = _RunPlanState()
        _states[run_id] = st
    return st


def mark_questions(run_id: str, payload: Dict[str, Any]) -> None:
    if run_id:
        _get(run_id).questions = payload


def get_questions(run_id: str) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    st = _states.get(run_id)
    return st.questions if st else None


def mark_plan(run_id: str, payload: Dict[str, Any]) -> None:
    if run_id:
        _get(run_id).plan = payload


def get_plan(run_id: str) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    st = _states.get(run_id)
    return st.plan if st else None


def set_todos(run_id: str, todos: List[Dict[str, Any]]) -> None:
    """Overwrite the todo snapshot (``update_todos`` always sends the full list)."""
    if run_id:
        _get(run_id).todos = todos


def get_todos(run_id: str) -> Optional[List[Dict[str, Any]]]:
    if not run_id:
        return None
    st = _states.get(run_id)
    return st.todos if st else None


def push_emit(run_id: str, item: Dict[str, Any]) -> None:
    """Queue a UI event (``{"event": <name>, "data": {...}}``) for the loop to drain."""
    if run_id:
        _get(run_id).emits.append(item)


def drain_emit(run_id: str) -> List[Dict[str, Any]]:
    if not run_id:
        return []
    st = _states.get(run_id)
    if not st or not st.emits:
        return []
    items = st.emits
    st.emits = []
    return items


def clear(run_id: str) -> None:
    _states.pop(run_id, None)
