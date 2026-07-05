"""In-memory 'pending user input' registry for event runs.

When the ``request_user_input`` tool runs inside an event-run turn it records
the question here, keyed by the stream ``run_id`` (readable by the tool via
``current_task_id_var``). The reasoning loop checks it to end the turn, and the
stream runner reads it at the terminal boundary to decide whether the run is
``pending`` (awaiting the user) vs ``completed``.

Purely transient: it only needs to survive from the tool call to the end of the
same turn. Durable pending state lives on the ``event_runs`` row.
"""

from __future__ import annotations

from typing import Dict, Optional

# run_id → the question the agent asked. Presence means "this turn parked
# pending".
_pending: Dict[str, str] = {}


def mark_pending(run_id: str, question: str) -> None:
    if run_id:
        _pending[run_id] = question or ""


def get_pending(run_id: str) -> Optional[str]:
    if not run_id:
        return None
    return _pending.get(run_id)


def clear(run_id: str) -> None:
    _pending.pop(run_id, None)
