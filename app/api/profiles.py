"""Profile management API."""

import asyncio
import json
import re
import urllib.parse
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import is_admin
from app.api.config import PROFILE_NAME_PATTERN
from app.auth import delete_token_file
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
            if await conversation_storage.profile_exists(name):
                return JSONResponse(
                    {"error": f"Profile '{name}' already exists"}, status_code=409,
                )

            profile = await conversation_storage.create_profile(name)
            ensure_persona_file(name)

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

            return JSONResponse(
                {
                    "success": True,
                    "message": f"Profile '{name}' created successfully",
                    "profile": profile["name"],
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

            return JSONResponse(
                {
                    "success": True,
                    "message": f"Profile '{profile_name}' deleted successfully",
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
            path="/api/profiles/{profile_name}",
            methods=["DELETE"], endpoint=handle_delete_profile,
        ),
    ]
