"""Status-file helpers for backup-create and restore progress.

Same durable-handoff pattern as :mod:`app.upgrade.status`: the work happens in a
detached subprocess (restore) or a background task (create), and progress lands
in a JSON file the API reads and streams over SSE. For restore specifically the
file is *the* handoff across the server restart — the new backend that boots
after the restart reads it to report the terminal result.

Two files under ``CREMIND_SYSTEM_DIR``:

- ``.backup.status.json``   — create-backup progress
  phases: ``queued|dumping|archiving|done|failed``
- ``.restore.status.json``  — restore progress
  phases: ``queued|validate|safety_backup|stage|restart|apply|migrate|done|failed``

Writes are atomic (tmp + rename); the only writer per file is the active
runner/task, readers are the API endpoints.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LOG_TAIL_MAX = 500
# Keep a recently-finished file around so the first poll after a server restart
# still observes the terminal state (restore restarts the backend; see
# app.upgrade.status.TERMINAL_GRACE_S for the same rationale).
TERMINAL_GRACE_S = 120

_RUNNING_EXCLUDED = (None, "idle", "done", "failed")


class StatusFile:
    """A single atomic JSON status file with phase + log-tail semantics."""

    def __init__(self, filename: str, kind: str):
        self._filename = filename
        self._kind = kind  # "backup" | "restore"

    def path(self) -> Path:
        from app.config.settings import BaseConfig

        return Path(BaseConfig.CREMIND_SYSTEM_DIR) / self._filename

    def _empty(self) -> dict[str, Any]:
        return {
            "kind": self._kind,
            "id": None,
            "phase": "idle",
            "ok": True,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "detail": {},
            "log_tail": [],
        }

    def read(self) -> dict[str, Any]:
        p = self.path()
        if not p.is_file():
            return self._empty()
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty()

    def write(self, state: dict[str, Any]) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def is_running(self) -> bool:
        return self.read().get("phase") not in _RUNNING_EXCLUDED

    def begin(self, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._empty()
        state.update(
            {
                "id": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "phase": "queued",
                "started_at": time.time(),
                "detail": detail or {},
            }
        )
        self.write(state)
        return state

    def update_phase(self, phase: str, message: str | None = None, *, ok: bool = True) -> None:
        state = self.read()
        state["phase"] = phase
        state["ok"] = ok
        if message:
            self._append_line(state, f"[{phase}] {message}")
        self.write(state)

    def append_log(self, line: str) -> None:
        state = self.read()
        self._append_line(state, line)
        self.write(state)

    def finish(self, *, ok: bool, error: str | None = None, detail: dict[str, Any] | None = None) -> None:
        state = self.read()
        state["phase"] = "done" if ok else "failed"
        state["ok"] = ok
        state["error"] = error
        state["finished_at"] = time.time()
        if detail:
            merged = dict(state.get("detail") or {})
            merged.update(detail)
            state["detail"] = merged
        self.write(state)

    def clear_if_terminal(self) -> None:
        p = self.path()
        if not p.is_file():
            return
        state = self.read()
        if state.get("phase") not in ("done", "failed"):
            return
        finished_at = state.get("finished_at")
        if isinstance(finished_at, (int, float)) and time.time() - finished_at < TERMINAL_GRACE_S:
            return
        try:
            p.unlink()
        except OSError:
            pass

    @staticmethod
    def _append_line(state: dict[str, Any], line: str) -> None:
        tail = state.setdefault("log_tail", [])
        tail.append(line)
        if len(tail) > LOG_TAIL_MAX:
            del tail[: len(tail) - LOG_TAIL_MAX]


backup_status = StatusFile(".backup.status.json", "backup")
restore_status = StatusFile(".restore.status.json", "restore")


def any_running() -> bool:
    """True if a create-backup or restore is currently in flight."""
    return backup_status.is_running() or restore_status.is_running()


__all__ = [
    "LOG_TAIL_MAX",
    "TERMINAL_GRACE_S",
    "StatusFile",
    "any_running",
    "backup_status",
    "restore_status",
]
