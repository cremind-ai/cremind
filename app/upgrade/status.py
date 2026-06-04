"""Status-file helpers for the detached in-app upgrade flow.

The HTTP server can't safely stream its own upgrade — the runner replaces
the wheel files in the live venv and restarts the process partway through,
which kills any open SSE/WS connection. So instead, the detached upgrade
subprocess writes to a status file at ``~/.cremind/.upgrade.status.json``
and the API just reads it. The file is the durable handoff: the new
backend that boots after restart reads the same file to report the
terminal result.

File layout (JSON):

    {
      "upgrade_id":      "<iso8601-utc>",
      "phase":           "queued|check|backup|install|migrate|health|restart|done|failed",
      "ok":              true,
      "current_version": "0.1.9",
      "target_version":  "0.1.10",
      "started_at":      1700000000.0,
      "finished_at":     null,
      "exit_code":       null,
      "error":           null,
      "log_tail":        ["line 1", "line 2", ...]   # capped at LOG_TAIL_MAX
    }

``LOG_TAIL_MAX`` matches the renderer's ``MAX_LOG_LINES`` in
``UpdateBanner.vue`` so the UI doesn't have to re-window what we
already trimmed.

Writes are atomic (write to ``.tmp`` then rename) so a reader never
sees a half-serialized file. Locking is not used; the only writer is
the detached subprocess and the API endpoints only read.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# Hold the tail at the same cap the renderer applies, so the UI never
# has to re-trim what the API returned.
LOG_TAIL_MAX = 500

# Grace window for clear_if_terminal: keep recently-finished status
# files so the renderer's first poll after a backend restart still
# observes the terminal state and transitions the UI. Under Docker the
# parent kill in detached.py triggers a container restart; by the time
# the new container's cremind serve runs clear_if_terminal, finished_at
# is typically only a few seconds old. 90 s is comfortably larger than
# any realistic container cold-start (db migrations + vncserver +
# cremind serve startup) on the supported platforms.
TERMINAL_GRACE_S = 90


def status_path() -> Path:
    """Resolve the status-file location under the working dir.

    Imported lazily because :mod:`app.config.settings` pulls in the full
    config stack — fine at request time, wasteful at module import.
    """
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / ".upgrade.status.json"


def _empty() -> dict[str, Any]:
    return {
        "upgrade_id": None,
        "phase": "idle",
        "ok": True,
        "current_version": None,
        "target_version": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "error": None,
        "log_tail": [],
    }


def read() -> dict[str, Any]:
    """Return the current status, or an ``idle`` placeholder if no file."""
    path = status_path()
    if not path.is_file():
        return _empty()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Treat a corrupt file as "no upgrade running" — better than
        # 500ing the API. The detached runner will overwrite it on the
        # next upgrade.
        return _empty()


def write(state: dict[str, Any]) -> None:
    """Atomically replace the status file with ``state``."""
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_running() -> bool:
    """True if an upgrade is currently in flight per the status file.

    Used by ``POST /apply`` to refuse a second concurrent upgrade with
    409. ``idle`` / ``done`` / ``failed`` all read as not-running.
    """
    return read().get("phase") not in (None, "idle", "done", "failed")


def begin(*, current_version: str, target_version: str) -> dict[str, Any]:
    """Initialise the status file at the start of an upgrade. Returns the new state."""
    state = _empty()
    state.update(
        {
            "upgrade_id": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phase": "queued",
            "current_version": current_version,
            "target_version": target_version,
            "started_at": time.time(),
        }
    )
    write(state)
    return state


def update_phase(phase: str, message: str | None = None, *, ok: bool = True) -> None:
    """Bump the current phase and optionally append a header line to the log."""
    state = read()
    state["phase"] = phase
    state["ok"] = ok
    if message:
        _append_line(state, f"[{phase}] {message}")
    write(state)


def append_log(line: str) -> None:
    """Append one line to the log tail, trimming to LOG_TAIL_MAX."""
    state = read()
    _append_line(state, line)
    write(state)


def finish(*, ok: bool, exit_code: int, error: str | None = None) -> None:
    """Mark the upgrade as terminated. ``phase`` flips to ``done`` or ``failed``."""
    state = read()
    state["phase"] = "done" if ok else "failed"
    state["ok"] = ok
    state["exit_code"] = exit_code
    state["error"] = error
    state["finished_at"] = time.time()
    write(state)


def clear_if_terminal() -> None:
    """Boot-time hook: drop the status file once the previous run is no
    longer needed for UI handoff.

    Leaves an in-flight upgrade alone (so a crashed subprocess can still
    be observed). Also leaves a recently-finished upgrade alone for
    ``TERMINAL_GRACE_S`` — under Docker the parent kill triggers a full
    container restart, and the renderer needs to poll ``/status`` once
    after reconnect to transition out of ``applying``. Deleting the file
    eagerly hides ``done`` behind a phantom ``idle`` and leaves the UI
    stuck. Called from server startup in ``app/server.py``.
    """
    path = status_path()
    if not path.is_file():
        return
    state = read()
    if state.get("phase") not in ("done", "failed"):
        return
    finished_at = state.get("finished_at")
    if isinstance(finished_at, (int, float)) and time.time() - finished_at < TERMINAL_GRACE_S:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _append_line(state: dict[str, Any], line: str) -> None:
    tail = state.setdefault("log_tail", [])
    tail.append(line)
    if len(tail) > LOG_TAIL_MAX:
        del tail[: len(tail) - LOG_TAIL_MAX]


__all__ = [
    "LOG_TAIL_MAX",
    "append_log",
    "begin",
    "clear_if_terminal",
    "finish",
    "is_running",
    "read",
    "status_path",
    "update_phase",
    "write",
]
