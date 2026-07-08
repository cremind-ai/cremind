"""Full-system Backup & Restore endpoints.

Registered **pre-storage** (see ``app/server.py``): restore must work in setup
mode (fresh install, storage not booted) and the restore status/stream endpoints
must answer immediately after the restore-triggered restart, before storage
comes up. Handlers resolve ``state.config_storage`` / ``state.storage_ready`` at
request time.

Create-backup runs as an in-process background task (a read-only snapshot vs.
the live system — no restart needed). Restore is split by mode:

- running system → spawn ``python -m app.backup.detached`` (Phase 1: validate,
  safety-backup, stage, then stop the server; Phase 2 applies on next boot).
- setup mode (fresh install) → apply in-process, then run the deferred boot.

Progress lands in status files (:mod:`app.backup.status`) polled by ``/status``
and streamed by ``/restore/stream`` — the same durable-handoff pattern as the
upgrade flow, chosen because the restore restarts the backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api._auth import require_admin, require_admin_or_setup_mode
from app.runtime import BootedState
from app.utils.logger import logger


def _config_storage(state: BootedState):
    return getattr(state, "config_storage", None)


# ── create ─────────────────────────────────────────────────────────────────


def _make_create_handler(state: BootedState):
    async def post_create(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied

        from app.backup import status as bstatus

        if not state.storage_ready:
            return JSONResponse({"error": "Storage is not ready."}, status_code=409)
        if bstatus.any_running():
            return JSONResponse(
                {"error": "A backup or restore is already running.",
                 "status_url": "/api/backup/status"},
                status_code=409,
            )

        passphrase = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                p = body.get("passphrase")
                if isinstance(p, str) and p.strip():
                    passphrase = p
        except Exception:  # noqa: BLE001
            passphrase = None

        asyncio.create_task(_run_create(passphrase))
        return JSONResponse(
            {"ok": True, "status_url": "/api/backup/status"}, status_code=202
        )

    return post_create


async def _run_create(passphrase: str | None) -> None:
    from app.backup import status as bstatus
    from app.backup.engine import BackupOptions, create_backup

    bstatus.backup_status.begin(detail={"encrypted": bool(passphrase)})

    def _progress(phase: str, cur: int, total: int) -> None:
        try:
            bstatus.backup_status.update_phase(phase)
        except Exception:  # noqa: BLE001
            pass

    try:
        bstatus.backup_status.update_phase("dumping", "Creating backup...")
        result = await asyncio.to_thread(
            create_backup, BackupOptions(passphrase=passphrase), _progress
        )
        bstatus.backup_status.finish(
            ok=True,
            detail={
                "name": result.path.name,
                "bytes": result.bytes_written,
                "file_count": result.file_count,
                "skipped": len(result.skipped),
            },
        )
        logger.info(f"[api:backup] create done: {result.path.name}")
    except Exception as e:  # noqa: BLE001
        logger.exception("[api:backup] create failed")
        bstatus.backup_status.finish(ok=False, error=str(e))


def _make_status_handler(state: BootedState):
    async def get_status(request: Request) -> JSONResponse:
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied
        from app.backup import status as bstatus

        return JSONResponse(bstatus.backup_status.read())

    return get_status


# ── list / download / delete / upload ────────────────────────────────────


def _make_list_handler(state: BootedState):
    async def get_list(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        from app.backup.engine import read_manifest
        from app.backup.store import list_archives, resolve_archive

        out = []
        for entry in list_archives():
            try:
                man = read_manifest(resolve_archive(entry["name"]))
                entry["manifest"] = man.summary()
            except Exception as e:  # noqa: BLE001
                entry["manifest"] = None
                entry["manifest_error"] = str(e)
            out.append(entry)
        return JSONResponse({"backups": out})

    return get_list


def _make_download_handler(state: BootedState):
    async def get_download(request: Request):
        denied = require_admin(request)
        if denied is not None:
            return denied
        from app.backup.store import resolve_archive

        name = request.path_params["name"]
        try:
            path = resolve_archive(name)
        except ValueError:
            return JSONResponse({"error": "Invalid backup name."}, status_code=400)
        if not path.is_file():
            return JSONResponse({"error": "Backup not found."}, status_code=404)
        return FileResponse(
            str(path), filename=name, media_type="application/octet-stream"
        )

    return get_download


def _make_delete_handler(state: BootedState):
    async def delete_backup(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        from app.backup.pending import read_pending
        from app.backup.store import resolve_archive

        name = request.path_params["name"]
        try:
            path = resolve_archive(name)
        except ValueError:
            return JSONResponse({"error": "Invalid backup name."}, status_code=400)

        pending = read_pending()
        if pending and pending.get("safety_backup_name") == name:
            return JSONResponse(
                {"error": "This is the safety backup for an in-progress restore."},
                status_code=409,
            )
        if not path.is_file():
            return JSONResponse({"error": "Backup not found."}, status_code=404)
        try:
            path.unlink()
        except OSError as e:
            return JSONResponse({"error": f"Could not delete: {e}"}, status_code=500)
        return JSONResponse({"ok": True})

    return delete_backup


def _make_upload_handler(state: BootedState):
    async def post_upload(request: Request) -> JSONResponse:
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied
        from app.backup.engine import read_manifest
        from app.backup.store import backups_root, safe_upload_name

        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "Missing 'file' upload."}, status_code=400)

        name = safe_upload_name(getattr(upload, "filename", "") or "upload")
        dest = backups_root() / name
        try:
            with open(dest, "wb") as f:
                while True:
                    chunk = await upload.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Upload failed: {e}"}, status_code=500)

        # Validate it's a real archive before accepting.
        try:
            man = read_manifest(dest)
        except Exception as e:  # noqa: BLE001
            try:
                dest.unlink()
            except OSError:
                pass
            return JSONResponse(
                {"error": f"Not a valid Cremind backup: {e}"}, status_code=400
            )

        size = dest.stat().st_size
        return JSONResponse(
            {"ok": True, "name": name, "size_bytes": size, "manifest": man.summary()}
        )

    return post_upload


# ── restore ────────────────────────────────────────────────────────────────


def _make_restore_handler(state: BootedState):
    async def post_restore(request: Request) -> JSONResponse:
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied

        from app.backup import status as bstatus
        from app.backup.pending import has_pending
        from app.backup.store import resolve_archive

        try:
            from app.upgrade import status as upgrade_status

            if upgrade_status.is_running():
                return JSONResponse(
                    {"error": "An upgrade is currently running."}, status_code=409
                )
        except ImportError:
            pass

        if has_pending() or bstatus.any_running():
            return JSONResponse(
                {"error": "A restore or backup is already in progress.",
                 "status_url": "/api/backup/restore/status"},
                status_code=409,
            )

        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        name = (body or {}).get("name")
        passphrase = (body or {}).get("passphrase")
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "Missing backup 'name'."}, status_code=400)
        try:
            archive = resolve_archive(name)
        except ValueError:
            return JSONResponse({"error": "Invalid backup name."}, status_code=400)
        if not archive.is_file():
            return JSONResponse({"error": "Backup not found."}, status_code=404)

        if state.storage_ready:
            return _spawn_detached_restore(archive, passphrase)
        return await _start_setup_restore(state, archive, passphrase)

    return post_restore


def _spawn_detached_restore(archive: Path, passphrase: str | None) -> JSONResponse:
    """Running system: spawn the Phase-1 detached restore runner."""
    from app.config.settings import BaseConfig

    cmd = [
        sys.executable, "-m", "app.backup.detached",
        "--archive", str(archive),
        "--parent-pid", str(os.getpid()),
    ]

    log_path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "restore-detached.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8", errors="replace")

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
            close_fds=True,
        )
        # Hand the passphrase over stdin (never argv) then close it.
        try:
            proc.stdin.write(((passphrase or "") + "\n").encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
    except OSError as e:
        log_handle.close()
        return JSONResponse(
            {"error": f"Failed to spawn restore runner: {e}"}, status_code=500
        )
    finally:
        try:
            log_handle.close()
        except OSError:
            pass

    return JSONResponse(
        {
            "ok": True,
            "mode": "restart_required",
            "pid": proc.pid,
            "status_url": "/api/backup/restore/status",
            "stream_url": "/api/backup/restore/stream",
        },
        status_code=202,
    )


async def _start_setup_restore(state: BootedState, archive: Path, passphrase: str | None) -> JSONResponse:
    """Fresh install (deferred storage): apply in-process, then boot."""
    from app.config.install_catalog import is_kubernetes_mode
    from app.config.bootstrap import bootstrap_exists

    if is_kubernetes_mode() and not bootstrap_exists():
        return JSONResponse(
            {
                "error": "On Kubernetes, configure the PostgreSQL database via the "
                "Setup Wizard first, then restore into it.",
            },
            status_code=409,
        )

    asyncio.create_task(_run_setup_restore(state, archive, passphrase))
    return JSONResponse(
        {
            "ok": True,
            "mode": "in_place",
            "status_url": "/api/backup/restore/status",
            "stream_url": "/api/backup/restore/stream",
        },
        status_code=202,
    )


async def _run_setup_restore(state: BootedState, archive: Path, passphrase: str | None) -> None:
    import time

    from app.backup import status as bstatus
    from app.backup.engine import read_manifest, restore_backup, verify_passphrase
    from app.backup.manifest import assert_restorable
    from app.backup.pending import write_report
    from app.config.bootstrap import bootstrap_exists, write_bootstrap
    from app.config.settings import BaseConfig

    bstatus.restore_status.begin(detail={"archive": archive.name, "mode": "setup"})
    try:
        bstatus.restore_status.update_phase("validate", "Validating the backup...")
        manifest = read_manifest(archive, passphrase)
        assert_restorable(manifest)
        if not verify_passphrase(archive, passphrase or ""):
            bstatus.restore_status.finish(ok=False, error="Wrong or missing passphrase.")
            return

        # Fresh non-K8s install with no provider choice yet → default to SQLite
        # so the next restart boots fully instead of re-entering setup mode.
        if not bootstrap_exists():
            write_bootstrap({"db_provider": "sqlite"})

        bstatus.restore_status.update_phase("apply", "Applying the restored data...")
        report = await asyncio.to_thread(
            restore_backup, archive, passphrase,
            target_system_dir=BaseConfig.CREMIND_SYSTEM_DIR,
        )
        bstatus.restore_status.update_phase("migrate", "Booting the restored system...")
        if state.boot_fn is not None:
            await state.boot_fn()

        write_report(
            {
                "restore_id": manifest.created_at or "setup",
                "ok": True,
                "finished_at": time.time(),
                "source": report.source,
                "db_row_counts": report.db_row_counts,
                "relocations": report.relocations,
                "file_count": report.file_count,
                "warnings": report.warnings,
                "safety_backup": None,
                "acknowledged": False,
            }
        )
        bstatus.restore_status.finish(
            ok=True, detail={"source": report.source, "file_count": report.file_count}
        )
        logger.info("[api:backup] setup-mode restore complete")
    except Exception as e:  # noqa: BLE001
        logger.exception("[api:backup] setup-mode restore failed")
        bstatus.restore_status.finish(ok=False, error=str(e))


def _make_restore_status_handler(state: BootedState):
    async def get_restore_status(request: Request) -> JSONResponse:
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied
        from app.backup import status as bstatus

        return JSONResponse(bstatus.restore_status.read())

    return get_restore_status


def _make_restore_stream_handler(state: BootedState):
    async def get_restore_stream(request: Request):
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied
        from app.backup import status as bstatus

        async def _events():
            last_seen = None
            while True:
                if await request.is_disconnected():
                    return
                st = bstatus.restore_status.read()
                fp = (st.get("phase"), len(st.get("log_tail") or []))
                if fp != last_seen:
                    last_seen = fp
                    yield (b"event: status\ndata: " + json.dumps(st).encode("utf-8") + b"\n\n")
                if st.get("phase") in ("done", "failed"):
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return get_restore_stream


# ── restore report + warnings ────────────────────────────────────────────


def _make_report_handler(state: BootedState):
    async def get_report(request: Request) -> JSONResponse:
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied
        from app.backup.pending import read_report

        report = read_report()
        warnings = {"autostart_failures": [], "disabled_channels": []}
        if state.storage_ready:
            warnings = _collect_live_warnings()
        return JSONResponse({"report": report, "warnings": warnings})

    return get_report


def _make_report_ack_handler(state: BootedState):
    async def post_ack(request: Request) -> JSONResponse:
        denied = require_admin_or_setup_mode(request, _config_storage(state))
        if denied is not None:
            return denied
        from app.backup.pending import ack_report

        return JSONResponse({"ok": ack_report()})

    return post_ack


def _collect_live_warnings() -> dict:
    autostart_failures = []
    disabled_channels = []
    try:
        from app.storage import get_autostart_storage

        for row in get_autostart_storage().list_all():
            if row.get("last_error"):
                autostart_failures.append(
                    {
                        "id": row.get("id"),
                        "profile": row.get("profile"),
                        "command": row.get("command"),
                        "working_dir": row.get("working_dir"),
                        "error": row.get("last_error"),
                    }
                )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[api:backup] autostart warnings unavailable: {e}")

    try:
        from sqlalchemy import text

        from app.databases import get_database_provider

        engine = get_database_provider().sync_engine()
        with engine.connect() as conn:
            # ``NOT enabled`` rather than ``enabled = 0`` so the comparison is
            # valid on both SQLite (0/1) and PostgreSQL (native boolean).
            rows = conn.execute(
                text(
                    "SELECT id, profile, channel_type, state FROM channels "
                    "WHERE NOT enabled AND channel_type != 'main'"
                )
            ).mappings()
            for r in rows:
                st = r["state"]
                if isinstance(st, str):
                    try:
                        st = json.loads(st)
                    except Exception:  # noqa: BLE001
                        st = {}
                last_error = (st or {}).get("last_error") if isinstance(st, dict) else None
                if last_error:
                    disabled_channels.append(
                        {
                            "id": r["id"],
                            "profile": r["profile"],
                            "channel_type": r["channel_type"],
                            "error": last_error,
                        }
                    )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[api:backup] channel warnings unavailable: {e}")

    return {"autostart_failures": autostart_failures, "disabled_channels": disabled_channels}


# ── registration ──────────────────────────────────────────────────────────


def get_backup_routes(state: BootedState) -> list[Route]:
    return [
        Route("/api/backup/create", _make_create_handler(state), methods=["POST"]),
        Route("/api/backup/status", _make_status_handler(state), methods=["GET"]),
        Route("/api/backup/list", _make_list_handler(state), methods=["GET"]),
        Route("/api/backup/download/{name}", _make_download_handler(state), methods=["GET"]),
        Route("/api/backup/upload", _make_upload_handler(state), methods=["POST"]),
        Route("/api/backup/restore", _make_restore_handler(state), methods=["POST"]),
        Route("/api/backup/restore/status", _make_restore_status_handler(state), methods=["GET"]),
        Route("/api/backup/restore/stream", _make_restore_stream_handler(state), methods=["GET"]),
        Route("/api/backup/restore/report", _make_report_handler(state), methods=["GET"]),
        Route("/api/backup/restore/report/ack", _make_report_ack_handler(state), methods=["POST"]),
        # DELETE last so the more specific paths above match first.
        Route("/api/backup/{name}", _make_delete_handler(state), methods=["DELETE"]),
    ]
