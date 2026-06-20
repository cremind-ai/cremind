"""Skill lifecycle API: delete / reset-to-default and import.

Complements ``app/api/tools.py`` (which handles skill *configuration*). Skills
live on disk at ``<CREMIND_SYSTEM_DIR>/<profile>/skills`` and surface as
``ToolType.SKILL`` tools in the registry.

- ``DELETE /api/skills/{tool_id}`` removes a skill's directory from the profile.
  For an external (user-imported) skill this is a permanent delete; for a
  built-in it is a "reset to default" — the shipped copy is immediately restored
  from ``app/skills/builtin`` (never deleted from source).
- ``POST /api/skills/import/archive`` installs skills from an uploaded archive.
- ``POST /api/skills/import/github`` installs skills from a public GitHub repo.

All blocking filesystem / network work runs in a thread via ``asyncio.to_thread``
so the event loop is never blocked. The registry is reconciled synchronously
(``resync_profile_skills``) so the Settings page updates immediately rather than
after the watcher's debounce window.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.runtime import BootedState
from app.skills.importer import (
    SkillImportError,
    install_archive,
    install_github,
    install_hub,
)
from app.skills.sync import (
    delete_profile_skill,
    is_builtin_skill_dir,
    reset_builtin_skill,
    resync_profile_skills,
)
from app.tools import ToolType
from app.tools.builtin.exec_shell_autostart import teardown_processes_for_dir
from app.utils.logger import logger

# Cap uploaded archives so a hostile upload can't exhaust disk before extraction.
_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024  # 100 MiB
_UPLOAD_CHUNK = 1 << 20  # 1 MiB


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _storage_not_ready() -> JSONResponse:
    return JSONResponse(
        {"error": "Setup not complete — storage is not ready yet."},
        status_code=503,
    )


def _clear_skill_config(registry, profile: str, tool_id: str) -> None:
    """Delete every saved config row (all scopes) for a skill's tool_id.

    Best-effort: logs and continues on failure so a config-clear hiccup never
    blocks the file-level reset. Returns the skill to its unconfigured default.
    """
    try:
        storage = registry.config.storage
        removed = storage.delete_all_configs(profile=profile, tool_id=tool_id)
        if removed:
            logger.info(
                f"Cleared {removed} config row(s) for reset skill '{tool_id}' "
                f"(profile '{profile}')"
            )
    except Exception:  # noqa: BLE001
        logger.exception(f"Failed to clear config for reset skill '{tool_id}'")


def get_skill_routes(state: BootedState) -> list[Route]:
    """Skill lifecycle routes (delete / reset / import)."""

    async def handle_delete_skill(request: Request) -> JSONResponse:
        """Delete an external skill, or reset a built-in skill to its default.

        The same on-disk delete underlies both; for a built-in the shipped copy
        is re-installed immediately so the reset takes effect without a reboot.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        tool_id = request.path_params["tool_id"]
        tool = registry.get(tool_id)
        if tool is None or tool.tool_type is not ToolType.SKILL:
            return JSONResponse(
                {"error": f"Skill '{tool_id}' not found"}, status_code=404
            )

        info = getattr(tool, "info", None)
        if info is None:
            return JSONResponse(
                {"error": "Skill has no backing directory"}, status_code=400
            )
        dir_name = info.dir_path.name
        builtin = is_builtin_skill_dir(dir_name)

        # Stop any long-running process the skill registered (and drop its
        # autostart row) before touching the directory. On Windows a running
        # listener holds file handles (e.g. scripts/.listener.lock) that would
        # otherwise make the delete/reset fail with WinError 32.
        try:
            await teardown_processes_for_dir(info.dir_path, profile=profile)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Process teardown failed for skill '{tool_id}'; proceeding anyway"
            )

        try:
            if builtin:
                await asyncio.to_thread(reset_builtin_skill, profile, dir_name)
                # Reset means pristine: also wipe the skill's saved config
                # (Skill Variables, arguments, ...). The files are restored from
                # the shipped built-in, but the per-profile config lives in the
                # DB keyed by tool_id — which survives because the directory path
                # (and thus the tool_id) is unchanged. Clear it explicitly.
                _clear_skill_config(registry, profile, tool_id)
            else:
                await asyncio.to_thread(delete_profile_skill, profile, dir_name)
                # External delete: the directory vanishes, so sync_skills drops
                # the tool row and ON DELETE CASCADE clears its config rows.
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Failed to delete/reset skill '{tool_id}'")
            return JSONResponse(
                {"error": f"Failed to {'reset' if builtin else 'delete'} skill: {exc}"},
                status_code=500,
            )

        await resync_profile_skills(profile, registry)
        return JSONResponse({"success": True, "reset": builtin})

    async def handle_import_archive(request: Request) -> JSONResponse:
        """Install skills from an uploaded archive (multipart form-data).

        Accepts a single file part under field name ``file`` (any field name is
        tolerated — the first file part wins).
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": f"Failed to parse upload: {exc}"}, status_code=400
            )

        upload = None
        for _field, value in form.multi_items():
            if hasattr(value, "filename") and getattr(value, "filename", None):
                upload = value
                break
        if upload is None:
            return JSONResponse({"error": "No archive file provided"}, status_code=400)

        filename = os.path.basename(upload.filename) or "skill-archive"
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="cremind-skill-upload-")
        tmp_path = Path(tmp_name)
        try:
            total = 0
            with os.fdopen(tmp_fd, "wb") as out:
                while True:
                    chunk = await upload.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ARCHIVE_BYTES:
                        return JSONResponse(
                            {"error": "Archive is too large (max 100 MiB)"},
                            status_code=413,
                        )
                    out.write(chunk)

            result = await asyncio.to_thread(install_archive, tmp_path, filename, profile)
        except SkillImportError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Skill archive import failed")
            return JSONResponse({"error": f"Import failed: {exc}"}, status_code=500)
        finally:
            tmp_path.unlink(missing_ok=True)

        await resync_profile_skills(profile, registry)
        return JSONResponse({"success": True, **result})

    async def handle_import_github(request: Request) -> JSONResponse:
        """Install skills from a public GitHub repository URL."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            return JSONResponse({"error": "'url' is required"}, status_code=400)

        try:
            result = await asyncio.to_thread(install_github, url, profile)
        except SkillImportError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Skill GitHub import failed")
            return JSONResponse({"error": f"Import failed: {exc}"}, status_code=500)

        await resync_profile_skills(profile, registry)
        return JSONResponse({"success": True, **result})

    async def handle_import_hub(request: Request) -> JSONResponse:
        """Install skills from a Cremind Hub link (skill page URL or bare name)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        link = body.get("link") or body.get("url")
        if not isinstance(link, str) or not link.strip():
            return JSONResponse({"error": "'link' is required"}, status_code=400)

        try:
            result = await asyncio.to_thread(install_hub, link, profile)
        except SkillImportError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Skill Hub import failed")
            return JSONResponse({"error": f"Import failed: {exc}"}, status_code=500)

        await resync_profile_skills(profile, registry)
        return JSONResponse({"success": True, **result})

    return [
        Route("/api/skills/import/archive", handle_import_archive, methods=["POST"]),
        Route("/api/skills/import/github", handle_import_github, methods=["POST"]),
        Route("/api/skills/import/hub", handle_import_hub, methods=["POST"]),
        Route("/api/skills/{tool_id}", handle_delete_skill, methods=["DELETE"]),
    ]
