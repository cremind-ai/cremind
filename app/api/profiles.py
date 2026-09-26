"""Profile management API."""

import asyncio
import json
import os
import re
import shutil
import time
import urllib.parse
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import is_admin
from app.api.config import PROFILE_NAME_PATTERN
from app.auth import delete_token_file
from app.config import working_dirs
from app.skills import initialize_profile_skills, teardown_profile_skills
from app.storage.conversation_storage import ConversationStorage
from app.tools import ToolRegistry
from app.utils import logger
from app.utils.agent_name import read_agent_name, write_agent_name
from app.utils.instructions import read_instructions_file, write_instructions_file
from app.utils.persona import ensure_persona_file, read_persona_file, write_persona_file


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_own_profile(
    request: Request, *, allow_admin: bool = False,
) -> tuple[str | None, JSONResponse | None]:
    """Authorize a ``{profile_name}``-scoped route.

    Returns ``(name, None)`` when the caller is authenticated and the URL's
    profile name is valid and owned by the caller; otherwise ``(None, error)``:

    * 401 — unauthenticated;
    * 400 — name missing, or not a valid profile name (bad characters / too
      long). A mis-slotted CLI argument (e.g. the persona *text* passed in the
      name position) now gets this clear 400 instead of a confusing 403;
    * 403 — a *valid* name that is not the caller's own profile. A token is
      scoped to exactly one profile (its JWT ``sub``), so it may only touch
      that profile.

    ``allow_admin=True`` widens the last check to "your own profile, or *any*
    profile if you are admin" — the same branch ``app.api.auth._resolve_target``
    makes for token rotation. It is opt-in per route because the personal
    endpoints (persona, instructions, agent name) are deliberately private even
    from admin; only administration of the profile itself (delete) takes it.
    The 401 → 400 → 403 ordering is unchanged either way: an unauthenticated or
    malformed call is rejected before admin-ness is ever consulted.
    """
    unauth = _require_auth(request)
    if unauth is not None:
        return None, unauth
    name = urllib.parse.unquote(request.path_params.get("profile_name", "") or "")
    if not name:
        return None, JSONResponse({"error": "Profile name is required"}, status_code=400)
    if not PROFILE_NAME_PATTERN.match(name) or len(name) > 64:
        return None, JSONResponse({"error": "Invalid profile name"}, status_code=400)
    own = _profile_from_request(request)
    if name != own and not (allow_admin and is_admin(request)):
        return None, JSONResponse(
            {"error": f"You can only modify your own profile ('{own}')."},
            status_code=403,
        )
    return name, None


# ── Working directory ──────────────────────────────────────────────────────
# Each profile has its own folder (app/config/working_dirs.py). Only admin may
# change one — any profile's — and a profile deleted with its folder at a
# Cremind-made location gets that folder archived (or, on request, removed).


def _working_dir_view(profile: str) -> dict[str, Any]:
    """``GET /api/profiles/{name}/working-dir``'s body. Creates nothing."""
    path = working_dirs.profile_working_dir(profile, create=False)
    return {
        "profile": profile,
        "path": path,
        "default_path": working_dirs.default_working_dir(profile),
        "is_default": (
            working_dirs.stored_working_dir(profile) is None
            or working_dirs.is_default_location(profile, path)
        ),
        "exists": os.path.isdir(path),
    }


def _invalid_working_dir_response(check: working_dirs.WorkingDirCheck) -> JSONResponse:
    return JSONResponse(
        {
            "error": "InvalidWorkingDir",
            "code": check.code,
            "message": check.message,
            "path": check.path,
        },
        status_code=400,
    )


def provision_new_profile_working_dir(
    profile: str, check: working_dirs.WorkingDirCheck | None = None,
) -> dict[str, Any]:
    """Give a profile being created its folder, and create it.

    ``check`` is the admin's validated choice, if any. Otherwise the default —
    unless a folder of that name already holds files (a same-name profile's
    leftovers, a stray copy), in which case a fresh ``<name>-2`` sibling is
    stored explicitly: a new profile never adopts someone else's files.
    Runs on the loop: ``set_working_dir`` notifies the change listeners."""
    explicit: str | None = None
    if check is not None and check.ok and not check.is_default:
        explicit = check.path
    else:
        explicit = working_dirs.fresh_default_for_new_profile(profile)
    if explicit:
        working_dirs.set_working_dir(profile, explicit)
    else:
        working_dirs.notify_changed(profile)
    path = working_dirs.profile_working_dir(profile, create=True)
    return {"path": path, "is_default": explicit is None}


def _cremind_made(profile: str, path: str) -> bool:
    """Is ``path`` a folder Cremind made for ``profile`` — its default, or the
    ``<name>-N`` sibling :func:`provision_new_profile_working_dir` stores when
    the default was taken? Only such a folder is ever moved or removed with
    its profile; one the admin chose elsewhere is never touched."""
    if working_dirs.is_default_location(profile, path):
        return True
    real = working_dirs.real_path(path)
    root = working_dirs.real_path(working_dirs.workspaces_root())
    if os.path.normcase(os.path.dirname(real)) != os.path.normcase(root):
        return False
    return re.fullmatch(re.escape(profile) + r"-\d+", os.path.basename(real)) is not None


def _is_empty_dir(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False


def _archive_sibling(path: str) -> str | None:
    """:func:`working_dirs.archive_default_dir` for a ``<name>-N`` sibling."""
    if not os.path.isdir(path):
        return None
    if _is_empty_dir(path):
        try:
            os.rmdir(path)
        except OSError:
            pass
        return None
    os.makedirs(working_dirs.deleted_root(), exist_ok=True)
    base = os.path.join(
        working_dirs.deleted_root(),
        f"{os.path.basename(path)}-{time.strftime('%Y%m%d-%H%M%S')}",
    )
    dst, n = base, 1
    while os.path.exists(dst):
        n += 1
        dst = f"{base}-{n}"
    os.replace(path, dst)
    return dst


def retire_working_dir(profile: str, path: str, *, delete: bool) -> dict[str, Any]:
    """What happens to a deleted profile's working directory — called after
    its row is gone. ``action`` is one of:

    * ``archived`` — moved to ``archived_to`` (``<workspaces>/.deleted/…``);
    * ``deleted`` — removed with its files (``delete=True``);
    * ``none`` — a Cremind-made folder that was missing or empty;
    * ``untouched`` — a folder the admin chose elsewhere, or one another
      profile still uses;
    * ``failed`` — the move or removal raised (``error``); the profile is
      deleted regardless.
    """
    result: dict[str, Any] = {"action": "untouched", "path": path}
    if not _cremind_made(profile, path):
        return result
    working_dirs.invalidate()
    # Another profile's folder IS this one. Not "owned" by name: left in
    # place, a ``<ws>/bob-2`` sibling would fall to a live profile ``bob-2``.
    if working_dirs.profiles_using(path):
        return result
    try:
        if working_dirs.is_default_location(profile, path):
            if delete:
                result["action"] = "deleted" if working_dirs.delete_default_dir(profile) else "none"
            else:
                moved = working_dirs.archive_default_dir(profile)
                result["action"] = "archived" if moved else "none"
                if moved:
                    result["archived_to"] = moved
        elif delete:
            if os.path.isdir(path):
                shutil.rmtree(path)
                result["action"] = "deleted"
            else:
                result["action"] = "none"
        else:
            moved = _archive_sibling(path)
            result["action"] = "archived" if moved else "none"
            if moved:
                result["archived_to"] = moved
    except OSError as exc:
        logger.exception(f"Could not retire the working directory {path!r} of deleted profile '{profile}'")
        result["action"] = "failed"
        result["error"] = str(exc)
    finally:
        working_dirs.invalidate()
    return result


def _delete_working_dir_mode(raw: object) -> str | None:
    """``keep`` (default) or ``delete``; None for anything else."""
    text = str(raw).strip().lower() if raw is not None else ""
    if not text:
        return "keep"
    return text if text in ("keep", "delete") else None


def get_profile_routes(
    conversation_storage: ConversationStorage,
    *,
    registry: ToolRegistry | None = None,
    drop_profile_embeddings: Callable[[str], None] | None = None,
) -> list[Route]:

    async def handle_list_profiles(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        try:
            profiles = await conversation_storage.list_profiles()
            visible = [p["name"] for p in profiles if not p["name"].startswith("__")]
            # This is the *management* roster — it drives the settings screen's
            # profile list, where every row is an object the caller may act on.
            # A token is scoped to one profile, so a non-admin only ever sees
            # its own. (The login dropdown's list of every name stays on the
            # public ``/api/profiles/names`` below, which is a different
            # contract: names, and nothing to act on.)
            if not is_admin(request):
                visible = [n for n in visible if n == _profile_from_request(request)]
            return JSONResponse({"profiles": visible}, status_code=200)
        except Exception as e:
            logger.error(f"Error listing profiles: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_list_profile_names(request: Request) -> JSONResponse:
        """Public (no auth): the login screen's profile dropdown.

        Deliberately exposes ONLY the visible profile names — no ids,
        timestamps, personas, or any other per-profile data. Anything
        richer must stay on the authenticated ``GET /api/profiles``.
        """
        try:
            profiles = await conversation_storage.list_profiles()
            visible = [p["name"] for p in profiles if not p["name"].startswith("__")]
            return JSONResponse({"profiles": visible}, status_code=200)
        except Exception as e:
            logger.error(f"Error listing profile names: {e}")
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    async def handle_add_profile(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        # Creating a profile is a tenancy decision, not a self-service one: a
        # new profile gets its own skills, tool rows and embedding table. Only
        # admin may make it. Checked after auth so an anonymous caller still
        # gets 401 (never a 403 that would confirm the endpoint exists), and
        # before validation so a non-admin learns nothing about name rules.
        # The first-run wizard does NOT come through here — it creates the
        # admin profile via ``POST /api/config/setup`` — so nothing bootstraps
        # itself into a chicken-and-egg with this gate.
        if not is_admin(request):
            return JSONResponse(
                {"error": "Only the admin profile can create profiles."},
                status_code=403,
            )
        try:
            body = await request.json()
            name = body.get("name")
            if not name:
                return JSONResponse({"error": "Profile name is required"}, status_code=400)
            if not re.match(r"^[a-z0-9_-]+$", name):
                return JSONResponse(
                    {"error": "Profile name must contain only lowercase letters, numbers, hyphens, and underscores"},
                    status_code=400,
                )
            # ``__…`` names are Cremind's own pseudo profiles (``__server__``):
            # such a name gets no working directory, so every turn would fail.
            if not working_dirs.valid_profile_dirname(name):
                return JSONResponse(
                    {"error": "Profile names cannot start with '__' (reserved for Cremind's internal profiles)"},
                    status_code=400,
                )
            # ``shared`` and ``cli`` are scope names of Cremind's own manual,
            # ``workspaces`` the folder holding every profile's working
            # directory (see app/cremind_documents/paths.py
            # RESERVED_PROFILE_NAMES).
            from app.cremind_documents.paths import reserved_profile_name_error

            reserved = reserved_profile_name_error(name)
            if reserved:
                return JSONResponse({"error": reserved}, status_code=400)
            if await conversation_storage.profile_exists(name):
                return JSONResponse(
                    {"error": f"Profile '{name}' already exists"}, status_code=409,
                )

            # Optional: the admin picks the new profile's working directory.
            # Validated (and created) before the row exists, so a refusal
            # leaves nothing behind but, at most, the folder it named.
            raw_working_dir = body.get("working_dir")
            if raw_working_dir is not None and not isinstance(raw_working_dir, str):
                return JSONResponse(
                    {"error": "'working_dir' must be a path or null"}, status_code=400,
                )
            working_dir_check = None
            if raw_working_dir and raw_working_dir.strip():
                working_dir_check = await asyncio.to_thread(
                    working_dirs.validate_working_dir, name, raw_working_dir, create=True,
                )
                if not working_dir_check.ok:
                    return _invalid_working_dir_response(working_dir_check)

            profile = await conversation_storage.create_profile(name)
            ensure_persona_file(name)

            try:
                working_dir = provision_new_profile_working_dir(name, working_dir_check)
            except Exception as exc:  # noqa: BLE001 — the profile exists; say what failed
                logger.exception(f"Could not set up the working directory of new profile '{name}'")
                working_dir = {"error": str(exc)}

            # Seed the new profile's skills directory with builtins and start
            # its watcher. This fires a registry change callback that builds
            # the per-profile embedding table.
            if registry is not None:
                try:
                    await initialize_profile_skills(
                        name, registry, loop=asyncio.get_running_loop(),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"Skill init failed for new profile '{name}'"
                    )

                # Backfill profile_tools rows for every existing a2a/mcp tool
                inserted = registry.on_profile_created(name)
                logger.info(
                    f"Backfilled {inserted} profile_tools row(s) for new profile '{name}'"
                )

            # Watch the new profile's own Cremind manual pages (its uuid folder
            # under storage/cremind_documents/profiles) now, not only after the
            # next restart. Off the loop: the watcher resolves the uuid and
            # creates the folder.
            try:
                from app.cremind_documents import get_service as get_manual_service
                from app.cremind_documents.watcher import start_scope_watcher

                manual_service = get_manual_service()
                if manual_service is not None:
                    await asyncio.to_thread(start_scope_watcher, manual_service, name)
            except Exception:  # noqa: BLE001 — the next boot starts it anyway
                logger.exception(f"Manual watcher failed for new profile '{name}'")

            return JSONResponse(
                {
                    "success": True,
                    "message": f"Profile '{name}' created successfully",
                    "profile": profile["name"],
                    "working_dir": working_dir,
                },
                status_code=201,
            )
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        except Exception as e:
            logger.error(f"Error adding profile: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_delete_profile(request: Request) -> JSONResponse:
        # Admin may delete any profile — it is the only profile with a view of
        # the others (see ``handle_list_profiles``), so without this branch a
        # profile could only ever be removed by its own token, which is exactly
        # the token that disappears with it.
        profile_name, err = _require_own_profile(request, allow_admin=True)
        if err is not None:
            return err
        # ...but never itself. ``admin`` is a literal name, not a role column
        # (``app.api._auth.is_admin``), so deleting it would leave an install
        # with no profile able to create, list or remove any other — an
        # unrecoverable state from the API alone. The web UI disables the row;
        # this makes it a server rule so the CLI can't get there either.
        if profile_name == "admin":
            return JSONResponse(
                {"error": "The admin profile cannot be deleted."}, status_code=403,
            )
        # What to do with the profile's working directory: ``keep`` (default —
        # a Cremind-made folder moves to ``<workspaces>/.deleted/``) or
        # ``delete``. A query parameter, or the same key in a JSON body.
        raw_mode = (getattr(request, "query_params", None) or {}).get("working_dir")
        if raw_mode is None:
            try:
                delete_body = await request.json()
            except Exception:  # noqa: BLE001 — no body is the usual case
                delete_body = None
            if isinstance(delete_body, dict):
                raw_mode = delete_body.get("working_dir")
        working_dir_mode = _delete_working_dir_mode(raw_mode)
        if working_dir_mode is None:
            return JSONResponse(
                {"error": "'working_dir' must be 'keep' or 'delete'"}, status_code=400,
            )
        # Read before the row goes: the folder the admin chose is stored on it.
        try:
            old_working_dir = working_dirs.profile_working_dir(profile_name, create=False)
        except ValueError:
            old_working_dir = None
        # The document index is keyed by the profile's uuid, which the delete
        # below removes with the row — read it first so the index can be found.
        documents_uid = None
        try:
            from app.storage.documents_storage import get_documents_storage

            documents_uid = await asyncio.to_thread(get_documents_storage().profile_uid, profile_name)
        except Exception:  # noqa: BLE001 — never block the delete
            logger.debug(f"Could not read the uuid of profile '{profile_name}'", exc_info=True)
        try:
            # Cascade FKs handle profile_tools / tool_configs / conversations / messages
            success = await conversation_storage.delete_profile(profile_name)
            if not success:
                return JSONResponse(
                    {"error": f"Profile '{profile_name}' not found"}, status_code=404,
                )

            # Drop the profile's token file. Not just tidiness: the serial that
            # backs revocation lives on the profile row, so recreating a profile
            # of the same name restarts it at 0 — and an orphaned token file
            # from the *old* profile would then validate against the new one.
            try:
                delete_token_file(profile_name)
            except Exception:  # noqa: BLE001 — never block the delete
                logger.exception(
                    f"Could not remove the token file for deleted profile '{profile_name}'"
                )

            # A Codex device-code sign-in for this profile may still be waiting
            # on the user's browser, holding an open ``codex app-server`` child
            # that would write a fresh ``auth.json`` into the tree we are about
            # to delete - after we deleted it. Cancel it first, so the order is
            # "stop producing credentials, then remove them" and not the other
            # way round. Its own try block: the login module is optional at
            # runtime (the Codex SDK may not be installed), and an ImportError
            # here must not cost us the removal below.
            try:
                from app.tools.builtin import codex_login

                await codex_login.cancel_for_profile(profile_name)
            except Exception:  # noqa: BLE001 - never block the delete
                logger.debug(
                    f"Could not cancel a pending Codex sign-in for deleted profile "
                    f"'{profile_name}'",
                    exc_info=True,
                )

            # The profile's coding-CLI logins live on disk, not in the DB: the
            # ``claude``/``codex`` CLIs write ``.credentials.json`` / ``auth.json``
            # under <SYSDIR>/<profile>/coding-cli, and both hold long-lived OAuth
            # refresh tokens in plaintext that nothing revokes upstream. Deleting
            # a profile deliberately does NOT remove its system directory (the
            # skills tree is kept so the data can be recovered), so unless this
            # runs the credential outlives the profile and rides out in every
            # later backup or ``~/.cremind`` copy - for a user who believes the
            # tenant is gone. Nothing else collects it either: the DB rows go by
            # FK cascade here, so the clean engine - the other place that pairs
            # the rows with the tree - never runs for a deletion.
            try:
                from app.config import coding_cli_homes

                await asyncio.to_thread(
                    coding_cli_homes.remove_profile_cli_homes, profile_name
                )
            except Exception:  # noqa: BLE001 - never block the delete
                logger.exception(
                    f"Could not remove the coding-CLI logins for deleted profile "
                    f"'{profile_name}'"
                )

            # Documentation search: settings rows cascade with the profile, but
            # its index file and vector collections live outside the database.
            try:
                from app.documents.service import forget_profile

                await asyncio.to_thread(forget_profile, profile_name, documents_uid)
            except Exception:  # noqa: BLE001 — never block the delete
                logger.exception(
                    f"Could not remove the document index of deleted profile '{profile_name}'"
                )

            # The Cremind manual pages this profile wrote live outside its
            # tree, at ``storage/cremind_documents/profiles/<uuid>`` (the uuid
            # read above — the row is gone now), and their points carry the
            # profile NAME as scope. Stop the watcher on that directory before
            # removing it, prune the points, and forget the cached uuid so a
            # profile re-created under this name gets its own, empty directory.
            # Never the shared scope's watcher: a profile named ``shared``
            # (possible before the name was reserved) shares that scope, and
            # stopping it would leave the bundled manual unwatched for every
            # profile. ``remove_profile_documents`` likewise never prunes it.
            # The retired ``cli`` scope needs no such guard: it has no watcher
            # of its own (one registered under ``cli`` can only be that
            # profile's, and must stop before its folder goes), and its points
            # are exactly what the boot prune removes anyway.
            try:
                from app.cremind_documents import get_service as get_manual_service
                from app.cremind_documents import remove_profile_documents
                from app.cremind_documents.paths import SHARED_SCOPE
                from app.cremind_documents.watcher import stop_scope_watcher

                if profile_name != SHARED_SCOPE:
                    await asyncio.to_thread(stop_scope_watcher, profile_name)
                await asyncio.to_thread(
                    remove_profile_documents, profile_name, documents_uid,
                    service=get_manual_service(),
                )
            except Exception:  # noqa: BLE001 — never block the delete
                logger.exception(
                    f"Could not remove the Cremind manual pages of deleted profile '{profile_name}'"
                )

            # Group memberships cascade away with the profile row, but the
            # runtime state of its seats (queue worker, stream bus, run
            # binding) and the in-memory group index do not — release them, or
            # a room keeps trying to hand messages to a tenant that is gone.
            try:
                from app.groups import boot as groups_boot

                await groups_boot.on_profile_deleted(profile_name)
            except Exception:  # noqa: BLE001 — never block the delete
                logger.exception(
                    f"Group-chat cleanup failed for deleted profile '{profile_name}'"
                )

            # Stop the watcher, drop the profile's skill rows, and remove the
            # per-profile embedding collection. The on-disk skills directory is
            # left intact so the profile's data can be recovered if desired.
            if registry is not None:
                try:
                    await teardown_profile_skills(
                        profile_name,
                        registry,
                        drop_embeddings=drop_profile_embeddings,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"Skill teardown failed for deleted profile '{profile_name}'"
                    )

            # The working directory, last: everything above that might hold
            # it open (watchers, the document index) has been stopped, and the
            # listeners hear about the profile before its folder moves — on
            # Windows a folder with an open watch handle cannot be moved.
            working_dir: dict[str, Any] | None = None
            try:
                working_dirs.notify_changed(profile_name)
                if old_working_dir:
                    working_dir = await asyncio.to_thread(
                        retire_working_dir, profile_name, old_working_dir,
                        delete=working_dir_mode == "delete",
                    )
            except Exception as exc:  # noqa: BLE001 — never block the delete
                logger.exception(
                    f"Working-directory cleanup failed for deleted profile '{profile_name}'"
                )
                working_dir = {"action": "failed", "path": old_working_dir, "error": str(exc)}

            return JSONResponse(
                {
                    "success": True,
                    "message": f"Profile '{profile_name}' deleted successfully",
                    "working_dir": working_dir,
                },
                status_code=200,
            )

        except Exception as e:
            logger.error(f"Error deleting profile: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_get_persona(request: Request) -> JSONResponse:
        profile_name, err = _require_own_profile(request)
        if err is not None:
            return err
        try:
            content = read_persona_file(profile_name)
            return JSONResponse({"content": content})
        except Exception as e:
            logger.error(f"Error reading persona for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_update_persona(request: Request) -> JSONResponse:
        profile_name, err = _require_own_profile(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        content = body.get("content")
        if content is None:
            return JSONResponse({"error": "'content' field is required"}, status_code=400)
        try:
            write_persona_file(profile_name, content)
            return JSONResponse({"success": True})
        except Exception as e:
            logger.error(f"Error writing persona for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_get_instructions(request: Request) -> JSONResponse:
        profile_name, err = _require_own_profile(request)
        if err is not None:
            return err
        try:
            return JSONResponse({"content": read_instructions_file(profile_name)})
        except Exception as e:
            logger.error(f"Error reading instructions for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_update_instructions(request: Request) -> JSONResponse:
        profile_name, err = _require_own_profile(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        content = body.get("content")
        if content is None:
            return JSONResponse({"error": "'content' field is required"}, status_code=400)
        try:
            write_instructions_file(profile_name, content)
            return JSONResponse({"success": True})
        except Exception as e:
            logger.error(f"Error writing instructions for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_list_agent_names(request: Request) -> JSONResponse:
        """Agent names for every visible profile — feeds the chat ``@`` menu."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        try:
            profiles = await conversation_storage.list_profiles()
            agents = [
                {"profile": p["name"], "name": read_agent_name(p["name"])}
                for p in profiles
                if not p["name"].startswith("__")
            ]
            return JSONResponse({"agents": agents}, status_code=200)
        except Exception as e:
            logger.error(f"Error listing agent names: {e}")
            return JSONResponse({"error": f"Internal server error: {e}"}, status_code=500)

    async def handle_get_agent_name(request: Request) -> JSONResponse:
        profile_name, err = _require_own_profile(request)
        if err is not None:
            return err
        try:
            return JSONResponse({"name": read_agent_name(profile_name)})
        except Exception as e:
            logger.error(f"Error reading agent name for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_update_agent_name(request: Request) -> JSONResponse:
        profile_name, err = _require_own_profile(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "'name' field is required"}, status_code=400)
        if len(name.strip()) > 128:
            return JSONResponse({"error": "Agent name must be at most 128 characters"}, status_code=400)
        try:
            write_agent_name(profile_name, name)
            return JSONResponse({"success": True})
        except Exception as e:
            logger.error(f"Error writing agent name for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_get_working_dir(request: Request) -> JSONResponse:
        """The profile's working directory — its owner, or admin, may read it."""
        profile_name, err = _require_own_profile(request, allow_admin=True)
        if err is not None:
            return err
        if not await conversation_storage.profile_exists(profile_name):
            return JSONResponse({"error": f"Profile '{profile_name}' not found"}, status_code=404)
        try:
            return JSONResponse(await asyncio.to_thread(_working_dir_view, profile_name))
        except Exception as e:
            logger.error(f"Error reading the working directory of '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_put_working_dir(request: Request) -> JSONResponse:
        """Set a profile's working directory. Admin only — for every profile,
        its own included: a profile's folder is where the other profiles'
        files are kept away from it, so it is not a self-service setting.

        Body ``{"path": "<absolute folder>"}``, or ``{"path": null}`` for the
        default (``<workspaces root>/<profile>``). The folder is created.
        """
        profile_name, err = _require_own_profile(request, allow_admin=True)
        if err is not None:
            return err
        if not is_admin(request):
            return JSONResponse(
                {"error": "Only the admin profile can change a profile's working directory."},
                status_code=403,
            )
        if not await conversation_storage.profile_exists(profile_name):
            return JSONResponse({"error": f"Profile '{profile_name}' not found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict) or "path" not in body:
            return JSONResponse(
                {"error": "'path' is required (null for the default folder)"}, status_code=400,
            )
        raw = body.get("path")
        if raw is not None and not isinstance(raw, str):
            return JSONResponse({"error": "'path' must be a string or null"}, status_code=400)
        check = await asyncio.to_thread(
            working_dirs.validate_working_dir, profile_name, raw, create=True,
        )
        if not check.ok:
            return _invalid_working_dir_response(check)
        try:
            # On the loop: it notifies the change listeners.
            working_dirs.set_working_dir(profile_name, None if check.is_default else check.path)
            return JSONResponse(await asyncio.to_thread(_working_dir_view, profile_name))
        except Exception as e:
            logger.error(f"Error setting the working directory of '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)


    return [
        Route(path="/api/profiles", methods=["GET"], endpoint=handle_list_profiles),
        Route(path="/api/profiles", methods=["POST"], endpoint=handle_add_profile),
        # Public (pre-auth): names only, for the login screen's dropdown.
        Route(path="/api/profiles/names", methods=["GET"], endpoint=handle_list_profile_names),
        # Agent names for the chat `@` menu (static path; before `{profile_name}`).
        Route(path="/api/profiles/agent-names", methods=["GET"], endpoint=handle_list_agent_names),
        Route(
            path="/api/profiles/{profile_name}/persona",
            methods=["GET"], endpoint=handle_get_persona,
        ),
        Route(
            path="/api/profiles/{profile_name}/persona",
            methods=["PUT"], endpoint=handle_update_persona,
        ),
        Route(
            path="/api/profiles/{profile_name}/instructions",
            methods=["GET"], endpoint=handle_get_instructions,
        ),
        Route(
            path="/api/profiles/{profile_name}/instructions",
            methods=["PUT"], endpoint=handle_update_instructions,
        ),
        Route(
            path="/api/profiles/{profile_name}/agent-name",
            methods=["GET"], endpoint=handle_get_agent_name,
        ),
        Route(
            path="/api/profiles/{profile_name}/agent-name",
            methods=["PUT"], endpoint=handle_update_agent_name,
        ),
        Route(
            path="/api/profiles/{profile_name}/working-dir",
            methods=["GET"], endpoint=handle_get_working_dir,
        ),
        Route(
            path="/api/profiles/{profile_name}/working-dir",
            methods=["PUT"], endpoint=handle_put_working_dir,
        ),
        Route(
            path="/api/profiles/{profile_name}",
            methods=["DELETE"], endpoint=handle_delete_profile,
        ),
    ]
