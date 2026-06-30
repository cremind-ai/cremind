"""Profile management API."""

import asyncio
import json
import re
import urllib.parse
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.skills import initialize_profile_skills, teardown_profile_skills
from app.storage.conversation_storage import ConversationStorage
from app.tools import ToolRegistry
from app.utils import logger
from app.utils.agent_name import read_agent_name, write_agent_name
from app.utils.persona import ensure_persona_file, read_persona_file, write_persona_file


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


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
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        try:
            profile_name = request.path_params.get("profile_name")
            if not profile_name:
                return JSONResponse({"error": "Profile name is required"}, status_code=400)
            profile_name = urllib.parse.unquote(profile_name)
            if profile_name != _profile_from_request(request):
                return JSONResponse({"error": "Forbidden"}, status_code=403)

            # Cascade FKs handle profile_tools / tool_configs / conversations / messages
            success = await conversation_storage.delete_profile(profile_name)
            if not success:
                return JSONResponse(
                    {"error": f"Profile '{profile_name}' not found"}, status_code=404,
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
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile_name = urllib.parse.unquote(request.path_params.get("profile_name", ""))
        if not profile_name:
            return JSONResponse({"error": "Profile name is required"}, status_code=400)
        if profile_name != _profile_from_request(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        try:
            content = read_persona_file(profile_name)
            return JSONResponse({"content": content})
        except Exception as e:
            logger.error(f"Error reading persona for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_update_persona(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile_name = urllib.parse.unquote(request.path_params.get("profile_name", ""))
        if not profile_name:
            return JSONResponse({"error": "Profile name is required"}, status_code=400)
        if profile_name != _profile_from_request(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403)
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
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile_name = urllib.parse.unquote(request.path_params.get("profile_name", ""))
        if not profile_name:
            return JSONResponse({"error": "Profile name is required"}, status_code=400)
        if profile_name != _profile_from_request(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        try:
            return JSONResponse({"name": read_agent_name(profile_name)})
        except Exception as e:
            logger.error(f"Error reading agent name for '{profile_name}': {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def handle_update_agent_name(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile_name = urllib.parse.unquote(request.path_params.get("profile_name", ""))
        if not profile_name:
            return JSONResponse({"error": "Profile name is required"}, status_code=400)
        if profile_name != _profile_from_request(request):
            return JSONResponse({"error": "Forbidden"}, status_code=403)
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
