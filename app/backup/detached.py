"""Detached restore runner — Phase 1 of a restore triggered while the server runs.

Invoked as a sibling subprocess of the running HTTP server::

    python -m app.backup.detached --archive <path> --parent-pid <PID>

(the passphrase, if any, is read from the first line of stdin — never argv, so
it can't leak into the process list).

The server can't apply a restore in-process: it must swap out the live DB and
file trees, which means no open handles / watchers, which means the server can't
be running. So this runner does everything that *can* be done while the server
is up — validate, take a safety backup, decrypt+stage the archive — writes a
pending-restore marker, and then stops the server. The actual apply happens on
the next boot (:func:`app.backup.pending.apply_pending_restore_if_any`). This is
one uniform path across native / Electron / Docker / K8s / bare ``cremind
serve``: the marker + staged files survive the restart on all of them.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path


def _kill_parent(pid: int) -> None:
    """Best-effort SIGTERM to the supervised backend so it restarts.

    Mirrors app.upgrade.detached._kill_parent. Under Docker/K8s this triggers a
    container restart; under a bare ``cremind serve`` the backend exits and the
    user relaunches (the marker + status file persist so the restore completes
    on the next boot).
    """
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        from app.backup.status import restore_status

        restore_status.append_log(
            f"[restart] could not signal pid {pid}: permission denied; "
            "restart the service manually to finish the restore."
        )


def _read_passphrase_from_stdin() -> str | None:
    try:
        line = sys.stdin.readline()
    except Exception:  # noqa: BLE001
        return None
    line = (line or "").rstrip("\r\n")
    return line or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cremind-restore-detached")
    parser.add_argument("--archive", required=True, help="Absolute path to the .cremind-backup archive.")
    parser.add_argument("--parent-pid", type=int, default=0, help="PID of the server to stop after staging.")
    args = parser.parse_args(argv)

    from app.backup import engine
    from app.backup.status import restore_status
    from app.backup import pending
    from app.utils import logger

    passphrase = _read_passphrase_from_stdin()
    archive = Path(args.archive)
    restore_id = time.strftime("%Y%m%d_%H%M%S", time.gmtime())

    restore_status.begin(detail={"archive": archive.name, "restore_id": restore_id})

    # 1. Validate manifest + passphrase (nothing mutated yet).
    try:
        restore_status.update_phase("validate", "Validating the backup archive...")
        manifest = engine.read_manifest(archive, passphrase)
        from app.backup.manifest import assert_restorable

        assert_restorable(manifest)
        if not engine.verify_passphrase(archive, passphrase or ""):
            restore_status.finish(ok=False, error="Wrong or missing passphrase for the encrypted backup.")
            return 1
    except Exception as e:  # noqa: BLE001
        logger.exception("[backup:restore] validation failed")
        restore_status.finish(ok=False, error=f"Backup is not restorable: {e}")
        return 1

    # 2. Safety backup of the current system (server still running — same live
    #    snapshot posture the upgrader uses before migrating).
    from app.backup.manifest import ARCHIVE_SUFFIX
    from app.backup.store import backups_root

    safety_path: Path | None = None
    try:
        restore_status.update_phase("safety_backup", "Backing up the current system before restoring...")
        safety_path = backups_root() / f"pre-restore-{restore_id}{ARCHIVE_SUFFIX}"
        engine.create_backup(engine.BackupOptions(dest=safety_path))
    except Exception as e:  # noqa: BLE001
        logger.exception("[backup:restore] safety backup failed")
        restore_status.finish(ok=False, error=f"Could not take a safety backup: {e}")
        return 1

    # 3. Decrypt + stage the archive (so Phase 2 needs no passphrase).
    try:
        restore_status.update_phase("stage", "Preparing the restore...")
        staging_dir = backups_root() / ".staging" / restore_id
        if staging_dir.exists():
            import shutil

            shutil.rmtree(staging_dir, ignore_errors=True)
        staged = engine.stage_backup(archive, passphrase, staging_dir)
    except Exception as e:  # noqa: BLE001
        logger.exception("[backup:restore] staging failed")
        restore_status.finish(ok=False, error=f"Could not prepare the restore: {e}")
        return 1

    # 4. Write the pending marker — the durable handoff to the next boot.
    pending.write_pending(
        {
            "restore_id": restore_id,
            "staging_dir": str(staged.staging_dir),
            "archive_name": archive.name,
            "safety_backup_path": str(safety_path) if safety_path else "",
            "safety_backup_name": safety_path.name if safety_path else "",
            "manifest_summary": staged.manifest.summary(),
            "verify_warnings": staged.verify_warnings,
            "requested_at": time.time(),
        }
    )

    # 5. Stop the server so the next boot applies the restore. Deliberately do
    #    NOT finish() — the terminal state is decided in Phase 2. Sleep briefly
    #    so the client's last /status poll lands before the listener dies.
    restore_status.update_phase("restart", "Restarting the service to apply the restore...")
    logger.info("[backup:restore] staged; stopping server to apply on next boot")
    time.sleep(2)
    _kill_parent(args.parent_pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
