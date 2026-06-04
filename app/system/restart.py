"""Detached helper that SIGTERMs the running Cremind backend.

Invoked as a sibling subprocess of the HTTP server:

    python -m app.system.restart --parent-pid <PID>

Mirrors the parent-kill half of :mod:`app.upgrade.detached` but trimmed:
no status file, no logging, no checks. The HTTP server can't kill
itself in a request handler — the connection would drop mid-response
and the client would see ECONNREFUSED with no warning. So
``POST /api/system/restart`` spawns this module detached and returns
202 immediately; this process waits a beat (so the response can reach
the client) and then sends SIGTERM to the parent.

What happens next is supervisor-dependent:

- Under Docker: ``restart: unless-stopped`` brings the container back.
- Under Electron: the main process's IPC restart handler is the
  preferred path (kills + respawns in-process); if the renderer falls
  back to this HTTP path, ``backendProcess.on('exit')`` fires but
  the main process does not auto-respawn — the user would see a
  dead backend.
- Under a bare ``cremind serve``: the backend exits and stays down.

The UI's confirmation dialog warns about these consequences before
the user gets here.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time


# Delay between spawn and SIGTERM. Picked so the API response that
# triggered us has comfortably reached the client before the listener
# dies; without this, the client sees ECONNREFUSED with no warning.
_RESTART_DELAY_S = 1.5


def _kill_parent(pid: int) -> None:
    """Best-effort SIGTERM of the supervised backend process."""
    if pid <= 0:
        return
    try:
        # Windows has no SIGTERM semantics, but ``os.kill(pid, SIGTERM)``
        # maps onto ``TerminateProcess`` which is what we want.
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Parent already gone — nothing to do.
        pass
    except PermissionError:
        # Should only happen if the API runs as a different user than
        # the backend; not a supported configuration. Silently skip.
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cremind-system-restart")
    parser.add_argument(
        "--parent-pid",
        type=int,
        required=True,
        help="PID of the HTTP server to SIGTERM after the response window.",
    )
    args = parser.parse_args(argv)

    time.sleep(_RESTART_DELAY_S)
    _kill_parent(args.parent_pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
