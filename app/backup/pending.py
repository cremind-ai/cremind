"""Pending-restore marker + boot-time apply + one-shot restore report.

Restore-while-running can't finish inside the running server (it must replace
the live DB and file trees, and under Docker/K8s the detached runner is killed
when the container restarts). So it is split:

- **Phase 1** (detached, server up): validate → safety-backup → stage the
  archive to a plaintext directory → write ``.restore.pending.json`` → stop the
  server. See :mod:`app.backup.detached`.
- **Phase 2** (this module, next ``cremind serve`` boot, before storage): apply
  the staged restore into the target's configured DB provider, migrate to head,
  write ``.restore.report.json``, then let the normal boot re-arm events,
  schedules, autostart processes, and channels. On failure it rolls back from
  the safety backup so a bad restore never bricks the server.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from app.backup.status import restore_status
from app.utils import logger

_PENDING_FILE = ".restore.pending.json"
_REPORT_FILE = ".restore.report.json"


def _sys_dir() -> Path:
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR)


def pending_path() -> Path:
    return _sys_dir() / _PENDING_FILE


def report_path() -> Path:
    return _sys_dir() / _REPORT_FILE


def _read_json(p: Path) -> dict[str, Any] | None:
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def read_pending() -> dict[str, Any] | None:
    return _read_json(pending_path())


def write_pending(data: dict[str, Any]) -> None:
    _write_json(pending_path(), data)


def clear_pending() -> None:
    try:
        pending_path().unlink()
    except OSError:
        pass


def read_report() -> dict[str, Any] | None:
    return _read_json(report_path())


def write_report(data: dict[str, Any]) -> None:
    _write_json(report_path(), data)


def ack_report() -> bool:
    rep = read_report()
    if rep is None:
        return False
    rep["acknowledged"] = True
    write_report(rep)
    return True


def has_pending() -> bool:
    return pending_path().is_file()


def apply_pending_restore_if_any() -> None:
    """Boot hook: apply a staged restore if one is pending.

    Safe to call unconditionally at startup — a no-op (plus a terminal-status
    sweep) when nothing is pending. Never raises: a failure rolls back from the
    safety backup and the server boots on the rolled-back state.
    """
    marker = read_pending()
    if not marker:
        restore_status.clear_if_terminal()
        return

    from app.config.settings import BaseConfig
    from app.databases import set_database_provider

    staging_dir = marker.get("staging_dir") or ""
    safety = marker.get("safety_backup_path") or ""
    target = BaseConfig.CREMIND_SYSTEM_DIR

    logger.info(f"[backup:restore] applying pending restore from {staging_dir}")
    try:
        restore_status.update_phase("apply", "Applying the restored data...")
        from app.backup.engine import apply_staged_restore

        set_database_provider(None)  # rebuild fresh from the target's bootstrap
        report = apply_staged_restore(Path(staging_dir), target_system_dir=target)

        restore_status.update_phase("migrate", "Schema is up to date.")
        _write_success_report(marker, report)
        restore_status.finish(
            ok=True,
            detail={
                "source": report.source,
                "file_count": report.file_count,
                "relocated": len(report.relocations.get("relocated", [])),
                "unmapped": len(report.relocations.get("unmapped", [])),
            },
        )
        logger.info("[backup:restore] pending restore applied successfully")
    except Exception as e:  # noqa: BLE001 — must never brick boot
        logger.exception("[backup:restore] pending restore FAILED; attempting rollback")
        rolled_back = _attempt_rollback(safety, target)
        _write_failure_report(marker, str(e), rolled_back)
        restore_status.finish(
            ok=False,
            error=f"{e}"
            + ("" if rolled_back else " (rollback also failed — check logs)"),
        )
    finally:
        clear_pending()
        _cleanup(staging_dir)


def _attempt_rollback(safety_backup_path: str, target: str) -> bool:
    if not safety_backup_path or not Path(safety_backup_path).is_file():
        logger.error("[backup:restore] no safety backup available to roll back to")
        return False
    try:
        restore_status.update_phase("apply", "Restore failed — rolling back to the pre-restore state...")
        from app.backup.engine import restore_backup
        from app.databases import set_database_provider

        set_database_provider(None)
        restore_backup(Path(safety_backup_path), target_system_dir=target)
        logger.info("[backup:restore] rollback to safety backup succeeded")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[backup:restore] rollback FAILED")
        return False


def _write_success_report(marker: dict[str, Any], report) -> None:
    write_report(
        {
            "restore_id": marker.get("restore_id"),
            "ok": True,
            "finished_at": time.time(),
            "source": report.source,
            "db_row_counts": report.db_row_counts,
            "relocations": report.relocations,
            "file_count": report.file_count,
            "warnings": report.warnings,
            "safety_backup": marker.get("safety_backup_name"),
            "acknowledged": False,
        }
    )


def _write_failure_report(marker: dict[str, Any], error: str, rolled_back: bool) -> None:
    write_report(
        {
            "restore_id": marker.get("restore_id"),
            "ok": False,
            "finished_at": time.time(),
            "source": marker.get("manifest_summary"),
            "error": error,
            "rolled_back": rolled_back,
            "safety_backup": marker.get("safety_backup_name"),
            "acknowledged": False,
        }
    )


def _cleanup(staging_dir: str) -> None:
    if staging_dir:
        shutil.rmtree(staging_dir, ignore_errors=True)


__all__ = [
    "ack_report",
    "apply_pending_restore_if_any",
    "clear_pending",
    "has_pending",
    "pending_path",
    "read_pending",
    "read_report",
    "report_path",
    "write_pending",
    "write_report",
]
