"""Blueprint API — export a profile's design, import it into a new profile.

Export endpoints are per-profile (``require_auth``; the caller's JWT ``sub`` is
the profile). Import endpoints are also ``require_auth`` — importing creates a
NEW profile, installs skill files, and registers listeners, all of which this
app already allows any authenticated profile to do (``handle_add_profile`` and
the skill/listener endpoints are ``require_auth`` too). Only one import runs at
a time server-wide, and each session is owner-scoped: a profile sees and
controls only the import it started (admin may see any). The import is a staged,
user-paced wizard: upload → per-step apply (synchronous responses; no SSE) →
finalize. The blueprint applies to the caller's current profile (the one whose
Blueprint page launched the wizard); there is no create-profile step. Aborting
stops the import and clears staging but leaves the profile intact — the user
creates a fresh profile beforehand if they don't want to change an existing one.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth
from app.utils.logger import logger

# One export at a time (the engine is CPU/IO light but avoids clobbering the
# module-level tar writer and gives the UI a clean "busy" signal).
_export_in_flight = False


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _owns(session, caller: str) -> bool:
    """Whether ``caller`` may see/control ``session`` (its owner, or admin)."""
    return session is not None and (session.owner == caller or caller == "admin")


def get_blueprint_routes(
    *,
    registry=None,
    conversation_storage=None,
    config_storage=None,
    drop_profile_embeddings=None,
) -> list[Route]:

    # ── export ────────────────────────────────────────────────────────────────

    async def handle_exportable(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        from app.blueprint.detect import collect_exportable

        try:
            payload = await asyncio.to_thread(collect_exportable, profile)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[blueprint] exportable detection failed")
            return JSONResponse({"error": "detect_failed", "message": str(exc)}, status_code=500)
        return JSONResponse(payload)

    async def handle_export(request: Request) -> JSONResponse:
        global _export_in_flight
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        components = set(body.get("components") or [])
        if not components:
            return JSONResponse(
                {"error": "no_components", "message": "Select at least one component to export."},
                status_code=400,
            )
        skills = body.get("skills")
        skill_slugs = set(skills) if isinstance(skills, list) else None
        tools = body.get("tools")
        tool_ids = set(tools) if isinstance(tools, list) else None
        settings = body.get("settings")
        setting_keys = set(settings) if isinstance(settings, list) else None
        events = body.get("events")
        event_ids = set(events) if isinstance(events, list) else None

        from app.blueprint.engine import ExportOptions, create_blueprint
        from app.blueprint.manifest import BlueprintError

        if _export_in_flight:
            return JSONResponse(
                {"error": "busy", "message": "Another export is already running."},
                status_code=409,
            )
        _export_in_flight = True
        try:
            options = ExportOptions(
                profile=profile,
                name=body.get("name") or body.get("display_name") or profile,
                display_name=body.get("display_name") or "",
                description=body.get("description") or "",
                author=body.get("author"),
                components=components,
                skill_slugs=skill_slugs,
                tool_ids=tool_ids,
                setting_keys=setting_keys,
                event_ids=event_ids,
            )
            result = await asyncio.to_thread(create_blueprint, options)
        except BlueprintError as exc:
            return JSONResponse({"error": "export_failed", "message": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[blueprint] export failed")
            return JSONResponse({"error": "export_failed", "message": str(exc)}, status_code=500)
        finally:
            _export_in_flight = False

        return JSONResponse(
            {
                "ok": True,
                "file": {"name": result.path.name, "bytes": result.bytes_written},
                "manifest": result.manifest.summary(),
                "warnings": result.warnings,
            }
        )

    async def handle_list(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        from app.blueprint.engine import read_blueprint_manifest
        from app.blueprint.store import list_archives, resolve_archive

        def _build() -> list[dict]:
            out = []
            for entry in list_archives():
                try:
                    manifest = read_blueprint_manifest(resolve_archive(entry["name"]))
                    summary = manifest.summary()
                except Exception:  # noqa: BLE001
                    summary = None
                # Scope: admin sees all; others see only their own exports.
                if summary and profile != "admin" and summary.get("source_profile") != profile:
                    continue
                out.append({**entry, "manifest": summary})
            return out

        archives = await asyncio.to_thread(_build)
        return JSONResponse({"blueprints": archives})

    async def handle_download(request: Request) -> Any:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        name = request.path_params["name"]
        from app.blueprint.engine import read_blueprint_manifest
        from app.blueprint.store import resolve_archive

        try:
            path = resolve_archive(name)
        except ValueError:
            return JSONResponse({"error": "invalid_name"}, status_code=400)
        if not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        if profile != "admin":
            try:
                manifest = read_blueprint_manifest(path)
                if manifest.source_profile != profile:
                    return JSONResponse({"error": "forbidden"}, status_code=403)
            except Exception:  # noqa: BLE001
                return JSONResponse({"error": "forbidden"}, status_code=403)
        return FileResponse(
            str(path), filename=name, media_type="application/octet-stream",
        )

    async def handle_delete(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        name = request.path_params["name"]
        from app.blueprint.engine import read_blueprint_manifest
        from app.blueprint.store import resolve_archive

        try:
            path = resolve_archive(name)
        except ValueError:
            return JSONResponse({"error": "invalid_name"}, status_code=400)
        if not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        if profile != "admin":
            try:
                manifest = read_blueprint_manifest(path)
                if manifest.source_profile != profile:
                    return JSONResponse({"error": "forbidden"}, status_code=403)
            except Exception:  # noqa: BLE001
                return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            path.unlink()
        except OSError as exc:
            return JSONResponse({"error": "delete_failed", "message": str(exc)}, status_code=500)
        return JSONResponse({"ok": True})

    # ── import ──────────────────────────────────────────────────────────────

    def _deps():
        from app.blueprint.apply import Deps

        return Deps(
            registry=registry,
            conversation_storage=conversation_storage,
            config_storage=config_storage,
            drop_profile_embeddings=drop_profile_embeddings,
        )

    def _busy_with_backup() -> JSONResponse | None:
        try:
            from app.backup.status import any_running

            if any_running():
                return JSONResponse(
                    {"error": "busy", "message": "A backup/restore is in progress; try again shortly."},
                    status_code=409,
                )
        except Exception:  # noqa: BLE001
            pass
        return None

    async def handle_import_upload(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        busy = _busy_with_backup()
        if busy is not None:
            return busy

        from app.blueprint.apply import abort as abort_session
        from app.blueprint.manifest import BlueprintError, BlueprintIncompatibleError
        from app.blueprint.session import find_active_session

        caller = _profile_from_request(request)
        replace = request.query_params.get("replace") == "true"
        existing = await asyncio.to_thread(find_active_session)
        if existing is not None:
            # One import runs at a time server-wide. Another profile's in-flight
            # import is opaque and must not be replaced from here.
            if not _owns(existing, caller):
                return JSONResponse(
                    {
                        "error": "session_busy",
                        "message": f"Another import (profile '{existing.owner}') is in progress.",
                    },
                    status_code=409,
                )
            if not replace:
                return JSONResponse(
                    {
                        "error": "session_exists",
                        "message": "An import is already in progress.",
                        "session_id": existing.id,
                    },
                    status_code=409,
                )
            await abort_session(existing, _deps(), delete_profile=True)

        try:
            form = await request.form()
        except Exception:
            return JSONResponse({"error": "invalid_form"}, status_code=400)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "missing_file"}, status_code=400)

        tmp = Path(tempfile.mkdtemp(prefix="cremind-bp-upload-"))
        saved = tmp / "upload.blueprint"
        try:
            data = await upload.read()
            saved.write_bytes(data)
            owner = _profile_from_request(request)
            session = await asyncio.to_thread(_stage, saved, owner)
        except BlueprintIncompatibleError as exc:
            return JSONResponse({"error": "incompatible", "message": str(exc)}, status_code=422)
        except BlueprintError as exc:
            return JSONResponse({"error": "bad_blueprint", "message": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[blueprint] upload/stage failed")
            return JSONResponse({"error": "stage_failed", "message": str(exc)}, status_code=500)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

        return JSONResponse(session.public_dict(), status_code=201)

    async def handle_import_hub(request: Request) -> JSONResponse:
        """Stage a blueprint downloaded from the Cremind Hub into the import wizard.

        Mirrors ``handle_import_upload`` (same one-import-at-a-time + replace
        semantics) but the archive is fetched from the hub instead of uploaded.
        """
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        busy = _busy_with_backup()
        if busy is not None:
            return busy

        from app.blueprint.apply import abort as abort_session
        from app.blueprint.hub import download_hub_blueprint
        from app.blueprint.manifest import BlueprintError, BlueprintIncompatibleError
        from app.blueprint.session import find_active_session

        caller = _profile_from_request(request)
        replace = request.query_params.get("replace") == "true"
        existing = await asyncio.to_thread(find_active_session)
        if existing is not None:
            if not _owns(existing, caller):
                return JSONResponse(
                    {
                        "error": "session_busy",
                        "message": f"Another import (profile '{existing.owner}') is in progress.",
                    },
                    status_code=409,
                )
            if not replace:
                return JSONResponse(
                    {
                        "error": "session_exists",
                        "message": "An import is already in progress.",
                        "session_id": existing.id,
                    },
                    status_code=409,
                )
            await abort_session(existing, _deps(), delete_profile=True)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_body"}, status_code=400)
        link = str(body.get("link") or "").strip()
        if not link:
            return JSONResponse({"error": "missing_link"}, status_code=400)

        tmp = Path(tempfile.mkdtemp(prefix="cremind-bp-hub-"))
        try:
            saved = await asyncio.to_thread(download_hub_blueprint, link, tmp)
            owner = _profile_from_request(request)
            session = await asyncio.to_thread(_stage, saved, owner)
        except BlueprintIncompatibleError as exc:
            return JSONResponse({"error": "incompatible", "message": str(exc)}, status_code=422)
        except BlueprintError as exc:
            return JSONResponse({"error": "bad_blueprint", "message": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[blueprint] hub import/stage failed")
            return JSONResponse({"error": "stage_failed", "message": str(exc)}, status_code=500)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

        return JSONResponse(session.public_dict(), status_code=201)

    async def handle_import_session(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        from app.blueprint.session import find_active_session

        caller = _profile_from_request(request)
        session = await asyncio.to_thread(find_active_session)
        if session is not None and not _owns(session, caller):
            session = None  # another profile's import is not visible here
        if session is None:
            # Fall back to the most recent terminal session so the report survives.
            sid = request.query_params.get("id")
            if sid:
                from app.blueprint.session import ImportSession

                loaded = ImportSession.load(sid)
                if loaded is not None and _owns(loaded, caller):
                    session = loaded
            if session is None:
                return JSONResponse({"error": "no_session"}, status_code=404)
        return JSONResponse(session.public_dict())

    async def handle_import_step(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        busy = _busy_with_backup()
        if busy is not None:
            return busy

        key = request.path_params["key"]
        is_skip = request.url.path.endswith("/skip")
        try:
            inputs = await request.json() if not is_skip else {}
        except Exception:
            inputs = {}

        from app.blueprint.apply import (
            StepError,
            apply_step,
        )
        from app.blueprint.session import (
            STATE_APPLYING,
            STEP_APPLIED,
            STEP_SKIPPED,
            TERMINAL_STATES,
            find_active_session,
        )

        caller = _profile_from_request(request)
        session = await asyncio.to_thread(find_active_session)
        if session is None or not _owns(session, caller):
            return JSONResponse({"error": "no_session"}, status_code=404)
        if session.state in TERMINAL_STATES:
            return JSONResponse({"error": "session_terminal"}, status_code=409)

        # Enforce step order.
        blocking = session.previous_incomplete(key)
        if blocking is not None:
            return JSONResponse(
                {"error": "out_of_order", "message": f"Complete the {blocking!r} step first."},
                status_code=409,
            )
        if session.step(key) is None:
            return JSONResponse({"error": "unknown_step", "message": key}, status_code=400)

        deps = _deps()
        session.state = STATE_APPLYING
        await asyncio.to_thread(session.save)
        try:
            # The blueprint applies to the caller's current profile (set at
            # stage time); there is no create-profile step.
            if session.target_profile is None:
                session.target_profile = caller
            result = await apply_step(session, key, inputs, deps)
        except StepError as exc:
            session.set_step_result(key, "failed", {"error": str(exc)})
            session.state = STATE_APPLYING
            await asyncio.to_thread(session.save)
            return JSONResponse(
                {"error": "step_failed", "message": str(exc), "retryable": True},
                status_code=422,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[blueprint] step {key} failed")
            session.set_step_result(key, "failed", {"error": str(exc)})
            await asyncio.to_thread(session.save)
            return JSONResponse(
                {"error": "step_failed", "message": str(exc), "retryable": True},
                status_code=500,
            )

        session.set_step_result(key, STEP_SKIPPED if is_skip else STEP_APPLIED, result)
        session.state = "staged"
        await asyncio.to_thread(session.save)
        return JSONResponse({"ok": True, "result": result, "session": session.public_dict()})

    async def handle_import_finalize(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        from app.blueprint.apply import finalize
        from app.blueprint.session import TERMINAL_STATES, find_active_session

        caller = _profile_from_request(request)
        session = await asyncio.to_thread(find_active_session)
        if session is None or not _owns(session, caller):
            return JSONResponse({"error": "no_session"}, status_code=404)
        if session.state in TERMINAL_STATES:
            return JSONResponse({"error": "session_terminal"}, status_code=409)
        if session.target_profile is None:
            session.target_profile = caller
        report = await asyncio.to_thread(finalize, session, _deps())
        return JSONResponse({"ok": True, "report": report})

    async def handle_import_abort(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        try:
            body = await request.json()
        except Exception:
            body = {}
        from app.blueprint.apply import abort as abort_session
        from app.blueprint.session import find_active_session

        caller = _profile_from_request(request)
        session = await asyncio.to_thread(find_active_session)
        if session is None or not _owns(session, caller):
            return JSONResponse({"error": "no_session"}, status_code=404)
        # The target profile pre-existed (the user created it), so aborting only
        # stops the import and clears staging — it never deletes the profile.
        # Anything already applied stays; the user can delete the profile if they
        # want a clean slate.
        delete_profile = bool(body.get("delete_profile", False))
        await abort_session(session, _deps(), delete_profile=delete_profile)
        return JSONResponse({"ok": True})

    async def handle_import_report(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        from app.blueprint.session import ImportSession, find_active_session

        caller = _profile_from_request(request)
        sid = request.query_params.get("id")
        session = ImportSession.load(sid) if sid else await asyncio.to_thread(find_active_session)
        if session is None or not _owns(session, caller) or session.report is None:
            return JSONResponse({"error": "no_report"}, status_code=404)
        return JSONResponse({"report": session.report})

    return [
        Route("/api/blueprints/exportable", handle_exportable, methods=["GET"]),
        Route("/api/blueprints/export", handle_export, methods=["POST"]),
        Route("/api/blueprints", handle_list, methods=["GET"]),
        Route("/api/blueprints/download/{name}", handle_download, methods=["GET"]),
        Route("/api/blueprints/import/upload", handle_import_upload, methods=["POST"]),
        Route("/api/blueprints/import/hub", handle_import_hub, methods=["POST"]),
        Route("/api/blueprints/import/session", handle_import_session, methods=["GET"]),
        Route("/api/blueprints/import/steps/{key}", handle_import_step, methods=["POST"]),
        Route("/api/blueprints/import/steps/{key}/skip", handle_import_step, methods=["POST"]),
        Route("/api/blueprints/import/finalize", handle_import_finalize, methods=["POST"]),
        Route("/api/blueprints/import/abort", handle_import_abort, methods=["POST"]),
        Route("/api/blueprints/import/report", handle_import_report, methods=["GET"]),
        # Registered last so the specific paths above win over the catch-all.
        Route("/api/blueprints/{name}", handle_delete, methods=["DELETE"]),
    ]


def _stage(saved: Path, owner: str):
    from app.blueprint.plan import stage_upload

    return stage_upload(saved, owner=owner)


__all__ = ["get_blueprint_routes"]
