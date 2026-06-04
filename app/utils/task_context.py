"""Per-asyncio-task context for the currently executing agent task.

The executor sets ``current_task_id_var`` at the start of each request so
downstream code (e.g. exec_shell's process registry) can tag work with the
originating task and cancellation can target it precisely.
"""

from contextvars import ContextVar
from typing import Optional

current_task_id_var: ContextVar[Optional[str]] = ContextVar(
    "cremind_current_task_id", default=None
)
