"""The skill's long-running app: one monitor per Cremind profile, plus its dashboard.

Cremind starts it as ``uv run scripts/event_listener.py`` (Events page, ``cremind
skill-events listener-start claude-usage-monitor``, or the skill's own ``dashboard``
command) and respawns it on every boot. It follows Cremind's listener contract: a
single instance per skill folder (an OS lock the kernel releases even on a crash), a
heartbeat file the Events page reads, and a clean stop on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any

from . import common as C
from .live import LivePoller
from .monitor import TICK_MS, Monitor
from .server import bind, serve_in_background

HEARTBEAT_S = 30.0  # Cremind calls a listener down once this file is 90 s old

_shutdown = threading.Event()
_instance_lock: Any = None


def _acquire_single_instance() -> bool:
    """Only one monitor per skill folder: an exclusive OS lock, released by the kernel when
    the process dies, so there are never stale locks."""
    global _instance_lock
    try:
        f = open(C.LOCK_FILE, "a+")  # noqa: SIM115 - held open for the process lifetime
    except OSError:
        return True  # can't create the lock file; don't block startup
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance_lock = f
    return True


def _touch_heartbeat() -> None:
    try:
        C.HEARTBEAT_FILE.touch()
    except OSError:
        pass


def _remove_runtime_files(paths: C.Paths, pid: int) -> None:
    try:
        C.HEARTBEAT_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    runtime = C.read_json(paths.runtime)
    if isinstance(runtime, dict) and runtime.get("pid") == pid:
        try:
            paths.runtime.unlink(missing_ok=True)
        except OSError:
            pass


def _install_signal_handlers() -> None:
    def handler(signum: int, frame: Any) -> None:
        _shutdown.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):  # SIGHUP: the console window was closed
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="event_listener.py", description="Claude Usage Monitor: the monitor and its dashboard.")
    parser.add_argument("--port", type=int, help="dashboard port (default: DASHBOARD_PORT from Settings, else 7337)")
    args = parser.parse_args(argv)

    _install_signal_handlers()
    if not _acquire_single_instance():
        # Cremind recognises "is already running for this skill" and treats the start as done.
        print(f"another claude-usage-monitor listener is already running for this skill (lock: {C.LOCK_FILE}); exiting", file=sys.stderr, flush=True)
        return 1

    paths = C.default_paths()
    paths.data.mkdir(parents=True, exist_ok=True)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    monitor = Monitor(paths)
    port = args.port or C.dashboard_port(C.load_env())
    try:
        server = bind(monitor, port)
    except OSError as e:
        print(f"Claude Usage Monitor could not start its dashboard: {e}", file=sys.stderr, flush=True)
        return 1
    monitor.dashboard_url = server.url
    pid = os.getpid()
    C.write_json_atomic(paths.runtime, {"pid": pid, "port": server.port, "url": server.url, "startedAt": C.now_ms()}, indent=2)
    _touch_heartbeat()
    atexit.register(_remove_runtime_files, paths, pid)
    serve_in_background(server)
    monitor.poller = LivePoller(monitor)  # official figures from Anthropic, in its own thread
    monitor.poller.start()

    moved = "" if server.port == port else f" (port {port} was taken)"
    print(f"Claude Usage Monitor running at {server.url}{moved}", flush=True)
    last_beat = time.monotonic()
    first = True
    try:
        while not _shutdown.is_set():
            started = time.monotonic()
            try:
                monitor.tick()
            except Exception:  # noqa: BLE001 - Cremind doesn't restart a crashed listener, so carry on
                monitor.warn("tick", traceback.format_exc(limit=8))
            if first:
                first = False
                names = ", ".join(rt.name for rt in monitor.profiles)
                print(f"{datetime.now():%H:%M:%S}  Tracking {len(monitor.profiles)} Claude Code profile{'' if len(monitor.profiles) == 1 else 's'}: {names}", flush=True)
            if time.monotonic() - last_beat >= HEARTBEAT_S:
                last_beat = time.monotonic()
                _touch_heartbeat()
            # Short waits keep Ctrl+C and SIGTERM responsive between ticks.
            remaining = TICK_MS / 1000 - (time.monotonic() - started)
            while remaining > 0 and not _shutdown.is_set():
                _shutdown.wait(min(remaining, 0.5))
                remaining = TICK_MS / 1000 - (time.monotonic() - started)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        # A switch runs in a request thread, which won't hold the process open: let it finish
        # moving the logins first.
        if monitor.switch_lock.acquire(timeout=30):
            monitor.switch_lock.release()
        monitor.poller.stop()
        monitor.close()
        _remove_runtime_files(paths, pid)
    print("Claude Usage Monitor stopped", flush=True)
    return 0
