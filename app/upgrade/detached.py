"""Detached upgrade runner — the body of an upgrade triggered via
``POST /api/upgrade/apply``.

Invoked as a sibling subprocess of the running HTTP server:

    python -m app.upgrade.detached --parent-pid <PID>

The HTTP server can't run :func:`app.upgrade.runner.apply` in-process
because the runner replaces its own wheel files on disk mid-flight. So
the API endpoint spawns *this* module detached, returns 202 immediately,
and lets the new process drive the install / migrate / restart cycle
on its own. Progress lands in the status file (see
:mod:`app.upgrade.status`) which the API endpoints poll.

After a successful upgrade the runner kills ``--parent-pid``. What
happens next depends on how the install is supervised:

- Under Docker: the entrypoint's ``wait -n`` exits when ``cremind serve``
  dies, Docker's restart policy brings the container back up, the
  entrypoint runs migrations again (idempotent) and re-launches the
  backend on the new wheel.
- Under Electron: the main process's ``runBackendUpgrade`` already
  handles the restart through its own IPC handler — for those installs
  the renderer uses the IPC bridge, not this detached path, so this
  module's parent-kill is a no-op (parent pid never gets passed).
- Under a bare ``cremind serve`` with no supervisor: the backend exits
  and the user must relaunch it. The status file remains so the new
  process surfaces the terminal result when it boots.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Callable

from app.__version__ import __version__ as CURRENT_VERSION
from app.upgrade import runner, status
from app.upgrade.runner import UpgradeEvent
from app.utils import logger


def _make_callback() -> tuple[Callable[[UpgradeEvent], None], dict[str, str | None]]:
    """Build the ``UpgradeEvent`` → status-file callback.

    Each event becomes a header line ``[<phase>] <message>`` plus a
    phase update so the renderer can render summary text without
    parsing log lines.

    Returns ``(callback, last_failure)``. ``last_failure["message"]``
    holds the most recent ``event.message`` where ``event.ok`` was False
    — so when ``runner.apply()`` returns False without raising, the
    caller can surface the actual reason instead of a hardcoded string.
    """
    last_failure: dict[str, str | None] = {"message": None}

    def _cb(event: UpgradeEvent) -> None:
        # Map runner kinds onto the broader status phase vocabulary.
        # ``check`` / ``backup`` / ``install`` / ``migrate`` / ``health``
        # / ``rollback`` are passed through; ``done`` and ``error`` are
        # handled by the caller via :func:`status.finish`.
        if event.kind in ("done", "error"):
            # finish() is called after apply() returns; don't double-mark.
            status.append_log(f"[{event.kind}] {event.message}")
            return
        if not event.ok:
            last_failure["message"] = event.message
        status.update_phase(event.kind, event.message, ok=event.ok)

    return _cb, last_failure


def _kill_parent(pid: int) -> None:
    """Best-effort kill of the supervised backend process.

    Used to trigger a Docker entrypoint restart (which re-runs
    migrations idempotently and serves the new wheel). On a host with
    no supervisor, this leaves the backend down — see module docstring.
    """
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            # Windows: no SIGTERM semantics; the entrypoint shell will
            # exit when the cremind.exe process dies. ``taskkill /F``
            # would be cleaner but adds a subprocess dependency; the
            # Python-native path below works for both PIDs.
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Parent already gone (e.g., user killed cremind serve between
        # apply call and restart). Status file already reflects success,
        # so the new boot will pick it up regardless.
        pass
    except PermissionError:
        # Should only happen if the API runs as a different user than
        # the backend, which is not a supported configuration. Log it
        # for the upgrade.log; the upgrade itself already succeeded.
        status.append_log(
            f"[restart] could not signal pid {pid}: permission denied; "
            "restart the backend manually to load the new version."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cremind-upgrade-detached")
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help=(
            "PID of the HTTP server that should be signalled after a "
            "successful upgrade so its supervisor relaunches it on the "
            "new wheel. 0 = don't kill anything."
        ),
    )
    parser.add_argument(
        "--target-version",
        default=None,
        help=(
            "Install this exact version instead of the latest. Test channel "
            "only — lets a tester switch to a specific PR's RC (possibly older "
            "than the current install). Ignored on production / dev."
        ),
    )
    args = parser.parse_args(argv)

    callback, last_failure = _make_callback()

    # Targeted test-channel install: skip the latest-based ``check`` gate
    # (the target may be older than the latest, which check() would report
    # as up_to_date) and let runner.apply resolve + validate the specific
    # release itself.
    if args.target_version:
        status.begin(current_version=CURRENT_VERSION, target_version=args.target_version)
        try:
            success = runner.apply(target_version=args.target_version, callback=callback)
        except Exception as e:  # noqa: BLE001
            logger.exception("[upgrade:detached] runner.apply (targeted) raised")
            status.finish(ok=False, exit_code=1, error=str(e))
            return 1
        if not success:
            logger.error("[upgrade:detached] targeted runner.apply returned False")
            status.finish(
                ok=False,
                exit_code=1,
                error=last_failure["message"] or "upgrade rolled back",
            )
            return 1
        return _finish_and_restart(args.parent_pid)

    # Resolve the target version from the runner's check pass; we
    # need it for the begin() banner. ``check`` is cheap and we want
    # the failure mode "GitHub unreachable" to land in the status file
    # too, not be swallowed by a startup crash.
    release, check_status = runner.check(callback=callback)
    target = release.version if release else "unknown"
    status.begin(current_version=CURRENT_VERSION, target_version=target)

    if check_status != "available":
        # Nothing to do — record the reason and exit cleanly. The API
        # surfaces this as ``phase=done, ok=true`` (up_to_date) or
        # ``phase=failed`` (unreachable / too_old).
        ok = check_status == "up_to_date"
        status.finish(
            ok=ok,
            exit_code=0 if ok else 1,
            error=None if ok else f"upgrade not applicable: {check_status}",
        )
        return 0 if ok else 1

    try:
        success = runner.apply(callback=callback)
    except Exception as e:  # noqa: BLE001
        logger.exception("[upgrade:detached] runner.apply raised")
        status.finish(ok=False, exit_code=1, error=str(e))
        return 1

    if not success:
        logger.error("[upgrade:detached] runner.apply returned False; upgrade rolled back")
        status.finish(
            ok=False,
            exit_code=1,
            error=last_failure["message"] or "upgrade rolled back",
        )
        return 1

    return _finish_and_restart(args.parent_pid)


def _finish_and_restart(parent_pid: int) -> int:
    """Mark the upgrade done and signal the parent to relaunch. Returns 0.

    Shared by the latest-based and targeted (test version-picker) paths.
    """
    # Tell the operator what's about to happen before we yank the
    # parent out from under the API request that triggered us. The
    # client may still be polling /status when the SIGTERM lands.
    status.update_phase("restart", "Restarting backend on the new version...")
    logger.info("[upgrade:detached] restart phase set")

    # Small delay so the API's most-recent /status response reaches the
    # client before the listener dies — without this, the SSE/poll just
    # sees ECONNREFUSED with no warning, and the renderer has to guess
    # what happened.
    logger.debug("[upgrade:detached] sleeping 2s pre-finish")
    time.sleep(2)

    # Mark done BEFORE killing the parent. Two reasons:
    #   1. Under Docker, the parent kill triggers a container restart;
    #      Docker SIGKILLs the detached process when the container exits,
    #      so a finish() that ran *after* the kill could be cut short and
    #      leave the file at phase="restart" forever.
    #   2. The new boot's clear_if_terminal preserves recently-finished
    #      states (see status.TERMINAL_GRACE_S), so the renderer's first
    #      poll after reconnect still sees "done" and transitions the UI.
    logger.info("[upgrade:detached] marking status finished ok=True")
    status.finish(ok=True, exit_code=0)
    logger.info(f"[upgrade:detached] sending SIGTERM to parent pid={parent_pid}")
    _kill_parent(parent_pid)
    logger.info("[upgrade:detached] _kill_parent returned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
