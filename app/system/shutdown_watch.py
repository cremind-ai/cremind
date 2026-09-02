"""The ``.shutdown.request`` sentinel — "please stop, gracefully".

``POST /api/system/restart`` runs inside the server, so it can ask for a
graceful shutdown by calling straight into it. The other initiators cannot:
the detached upgrade and restore runners are separate processes, and the
server they need to stop is their parent. Signalling it is what they used to
do, and on Windows that means ``TerminateProcess`` — the hard kill this whole
handshake exists to avoid.

So they drop a sentinel file in the system dir instead, and the running server
watches for it. Presence is the entire protocol: the JSON body is a debugging
breadcrumb (who asked, when) and is never parsed by the watcher. The file is
deleted by whoever consumes it, and any leftover is cleared at the next boot —
acting on a stale one would stop a server nobody asked to stop.

The runners still wait for the parent to exit and fall back to a signal if it
does not, which is what carries the first upgrade *from* a build whose server
predates this module.

Stdlib-only, with ``system_dir`` passed in rather than read from
``app.config`` — same discipline as :mod:`app.system.boot_service`, so the CLI
can import this without dragging the server's settings in.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable


#: Sentinel filename, relative to the system dir.
SHUTDOWN_REQUEST_FILE = ".shutdown.request"

#: How often the watcher looks. A shutdown is not latency-critical to within a
#: second, and the runners' grace budget absorbs this comfortably.
DEFAULT_POLL_INTERVAL_S = 1.0


def shutdown_request_path(system_dir: Path | str) -> Path:
    return Path(system_dir) / SHUTDOWN_REQUEST_FILE


def write_shutdown_request(system_dir: Path | str, *, source: str) -> None:
    """Ask the server running out of ``system_dir`` to shut down gracefully.

    Written atomically so the watcher can never observe a half-file: it acts
    on the rename, which is all-or-nothing.
    """
    path = shutdown_request_path(system_dir)
    payload = {
        "source": source,
        "pid": os.getpid(),
        "requested_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def clear_stale_shutdown_request(system_dir: Path | str) -> None:
    """Drop a sentinel left over from a previous run. Best-effort."""
    try:
        shutdown_request_path(system_dir).unlink()
    except OSError:
        pass


class ShutdownRequestWatcher:
    """Polls for the sentinel and calls ``on_request`` once.

    Single-shot on purpose: once a shutdown is in flight there is nothing a
    second request could add, and the initiator's own escalation covers the
    case where the shutdown wedges.
    """

    def __init__(
        self,
        system_dir: Path | str,
        on_request: Callable[[], None],
        *,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._path = shutdown_request_path(system_dir)
        self._on_request = on_request
        self._poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="cremind-shutdown-request-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop watching. Safe to call when never started, or twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._path.is_file():
                # Consume BEFORE acting: a crash between the two must not leave
                # a sentinel that stops the next server the moment it boots.
                try:
                    self._path.unlink()
                except OSError:
                    pass
                try:
                    self._on_request()
                except Exception:  # noqa: BLE001 - nothing above us to report to
                    pass
                return
            self._stop.wait(self._poll_interval_s)
