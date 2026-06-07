"""Tool management API.

Replaces the old per-type ``/api/tools`` + ``/api/agents`` + ``/api/mcp-servers``
endpoints with a unified surface keyed by ``tool_id``. Built-in and skill
configuration is also exposed here.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth_or_setup_mode
from app.config.settings import BaseConfig
from app.events.settings_state_bus import publish_settings_state_changed
from app.lib.llm.factory import create_llm_provider
from app.runtime import BootedState
from app.skills.sync import is_builtin_skill_dir
from app.storage import get_autostart_storage
from app.tools import ToolType
from app.tools.builtin import (
    BuiltInToolGroup,
    get_builtin_tool_config,
    list_builtin_tool_catalog,
    refresh_builtin_tool_oauth,
)
from app.tools.builtin.exec_shell import publish_process_list_changed
from app.tools.builtin.exec_shell_autostart import (
    normalize_command_paths,
    spawn_from_autostart,
)
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _storage_not_ready() -> JSONResponse:
    """503 response for write handlers invoked before storage is booted."""
    return JSONResponse(
        {"error": "Setup not complete — storage is not ready yet."},
        status_code=503,
    )


def get_tool_routes(state: BootedState) -> list[Route]:
    """Tool-management routes.

    Registered pre-storage so the Setup Wizard can render the tool catalog
    before the registry has been built. Handlers resolve ``state.registry``,
    ``state.config_storage``, and ``state.connect_persisted_tool`` at
    request time; ``handle_list_tools`` falls back to
    :func:`list_builtin_tool_catalog` when the registry is not yet
    available. Write handlers stay auth-gated and return 503 if invoked
    before storage is ready.
    """

    async def handle_list_tools(request: Request) -> JSONResponse:
        """List all tools visible to the profile (excluding hidden intrinsic tools).

        Each row carries enough metadata for the UI to render the
        configuration form without an extra round-trip:
        - ``required_fields`` -- built-in tool required_config schema
        - ``config`` -- current per-profile values (variables/arguments/llm/meta)
        - ``full_reasoning`` -- mirrored from ``config.llm.full_reasoning`` so
          the frontend doesn't have to know about the scoping
        - ``locked_llm_fields`` -- LLM-parameter keys whose user-facing
          override is forbidden (the UI disables them and the API rejects
          writes)

        Open during first-run setup (no JWT, no profile) so the wizard
        can render the tool catalog with default enabled flags and empty
        per-profile snapshots; gated post-setup.
        """
        registry = state.registry
        config_storage = state.config_storage
        if config_storage is not None:
            denied = require_auth_or_setup_mode(request, config_storage)
            if denied is not None:
                return denied
        # Deferred-storage window: no registry yet. Return the static built-in
        # catalog so the wizard's Tools step renders before the user clicks
        # "Apply" (which is what materialises storage and the registry).
        if registry is None:
            return JSONResponse({"tools": list_builtin_tool_catalog()})
        config_manager = registry.config
        profile = _profile_from_request(request)
        if not profile and config_storage is not None and config_storage.is_setup_complete():
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        rows = registry.visible_for_profile(profile)
        enriched: list[dict] = []
        for row in rows:
            tool = registry.get(row["tool_id"])
            schema = _schema_for_tool(tool)
            required_fields = schema.get("tool", {}).get("required_config", {})
            snapshot = config_manager.snapshot(row["tool_id"], profile)
            row.update({
                "configured": _is_tool_configured(tool, snapshot),
                "config": snapshot,
                "required_fields": required_fields,
                "llm_defaults": _llm_defaults_for_tool(tool),
                "locked_llm_fields": _locked_llm_fields_for_tool(tool),
                "full_reasoning": bool(snapshot.get("llm", {}).get("full_reasoning", False)),
                # Optional pip-extras feature key (built-in tools only).
                # Drives the Setup Wizard "Installs: cremind[…]" hint and
                # the post-setup enable-toggle install dialog.
                "requires_feature": schema.get("tool", {}).get("requires_feature"),
            })
            if hasattr(tool, "connection_error") and getattr(tool, "connection_error"):
                row["connection_error"] = tool.connection_error
                row["is_stub"] = True
            else:
                row["is_stub"] = bool(getattr(tool, "is_stub", False))
            if hasattr(tool, "is_llm_bound"):
                row["llm_bound"] = bool(tool.is_llm_bound)
            if hasattr(tool, "url"):
                row["url"] = tool.url
            if hasattr(tool, "owner_profile"):
                row["owner_profile"] = tool.owner_profile
            # Skills carry a built-in flag so the Settings page can offer
            # "Reset to Default" for shipped skills vs "Delete" for imported ones.
            if tool.tool_type is ToolType.SKILL:
                info = getattr(tool, "info", None)
                row["is_builtin"] = bool(
                    info is not None and is_builtin_skill_dir(info.dir_path.name)
                )
            lra = _long_running_app_for_tool(tool)
            if lra is not None:
                row["long_running_app"] = lra
            enriched.append(row)
        return JSONResponse({"tools": enriched})

    async def handle_get_tool(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        config_manager = registry.config
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool_id = request.path_params["tool_id"]
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        schema = _schema_for_tool(tool)
        snapshot = config_manager.snapshot(tool_id, profile)
        payload = {
            "tool_id": tool_id,
            "name": tool.name,
            "tool_type": tool.tool_type.value,
            "description": tool.description,
            "arguments_schema": tool.arguments_schema,
            "schema": schema,
            "config": snapshot,
            "llm_defaults": _llm_defaults_for_tool(tool),
            "locked_llm_fields": _locked_llm_fields_for_tool(tool),
            "configured": _is_tool_configured(tool, snapshot),
        }
        lra = _long_running_app_for_tool(tool)
        if lra is not None:
            payload["long_running_app"] = lra
        return JSONResponse(payload)

    async def handle_set_variables(request: Request) -> JSONResponse:
        """Update Tool Variables (env-style secrets) for a tool."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        config_manager = registry.config
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        variables = body.get("variables", {})
        if not isinstance(variables, dict):
            return JSONResponse({"error": "'variables' must be an object"}, status_code=400)

        tool = registry.get(tool_id)
        schema = _schema_for_tool(tool) if tool else {}
        required_config = schema.get("tool", {}).get("required_config", {})

        for key, value in variables.items():
            field_spec = required_config.get(key, {})
            is_secret = bool(
                field_spec.get("secret")
                or "secret" in key.lower()
                or "key" in key.lower()
                or "password" in key.lower()
            )
            # Normalize native JSON booleans to lowercase strings so
            # downstream consumers (e.g. _coerce_headless) see "true"/"false"
            # instead of Python's str(True) -> "True".
            if isinstance(value, bool):
                value = "true" if value else "false"
            config_manager.set_variable(
                tool_id, profile, key, str(value), is_secret=is_secret,
            )

        # Refresh OAuth client for built-in tools that just got their credentials
        if tool and tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
            refresh_builtin_tool_oauth(registry, config_manager, tool_id, profile=profile)

        # Mirror skill variables to ``{skill_dir}/scripts/.env`` so the skill's
        # scripts can source them at runtime — the agent has no per-skill hook
        # to prepare the environment for the generic exec_shell tool.
        if tool and tool.tool_type is ToolType.SKILL:
            _write_skill_env_file(tool, config_manager, profile)

        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    async def handle_set_arguments(request: Request) -> JSONResponse:
        """Update Tool Arguments (JSON-Schema parameter values)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        config_manager = registry.config
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "'arguments' must be an object"}, status_code=400)
        config_manager.set_arguments(tool_id, profile, arguments)
        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    async def handle_set_llm_params(request: Request) -> JSONResponse:
        """Update LLM Parameters (provider, model, full_reasoning, reasoning_effort)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        config_manager = registry.config
        config_storage = state.config_storage
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        llm_params = body.get("llm", {})
        if not isinstance(llm_params, dict):
            return JSONResponse({"error": "'llm' must be an object"}, status_code=400)

        tool = registry.get(tool_id)
        locked = set(_locked_llm_fields_for_tool(tool))
        if locked:
            llm_defaults = _llm_defaults_for_tool(tool)
            for key in locked:
                if key in llm_params and llm_params[key] != llm_defaults.get(key):
                    return JSONResponse(
                        {
                            "error": (
                                f"LLM parameter '{key}' is locked for tool "
                                f"'{tool_id}' and cannot be modified."
                            )
                        },
                        status_code=400,
                    )

        for key, value in llm_params.items():
            config_manager.set_llm_param(tool_id, profile, key, value)

        # If provider/model/reasoning_effort changed, rebuild the adapter's LLM.
        # Read the *post-write* DB values so partial updates (e.g. only
        # reasoning_effort in the body) are merged with the existing settings.
        if tool and hasattr(tool, "update_runtime_config") and any(
            k in llm_params for k in ("llm_provider", "llm_model", "reasoning_effort")
        ):
            try:
                merged = config_manager.get_llm_params(tool_id, profile)
                provider_name = merged.get("llm_provider") or None
                model_name = merged.get("llm_model") or None

                # When no per-tool model override, fall back to the "low"
                # model group (same source as startup in server.py).
                if not model_name:
                    group_value = BaseConfig.get_model_group("low", profile=profile)
                    if group_value:
                        parts = group_value.split("/", 1)
                        if not provider_name:
                            provider_name = parts[0]
                        model_name = parts[1] if len(parts) > 1 else parts[0]

                if not provider_name:
                    provider_name = BaseConfig.get_default_provider(profile=profile)

                if model_name:
                    new_llm = create_llm_provider(
                        provider_name=provider_name,
                        model_name=model_name,
                        config_storage=config_storage,
                        profile=profile,
                        default_reasoning_effort=merged.get("reasoning_effort"),
                    )
                    tool.update_runtime_config(llm=new_llm)
                else:
                    logger.warning(
                        f"No model resolved for tool '{tool_id}'; skipping LLM rebuild"
                    )
            except ValueError as e:
                return JSONResponse({"error": f"Invalid LLM: {e}"}, status_code=400)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"Failed to apply LLM change to running tool '{tool_id}'"
                )
                return JSONResponse(
                    {"error": f"Failed to apply LLM change: {e}"}, status_code=500,
                )

        # If full_reasoning changed, propagate to the running adapter
        if tool and "full_reasoning" in llm_params and hasattr(tool, "update_runtime_config"):
            tool.update_runtime_config(full_reasoning=bool(llm_params["full_reasoning"]))

        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    async def handle_reset_llm_params(request: Request) -> JSONResponse:
        """Delete selected LLM-parameter overrides so code defaults apply again.

        Body: ``{"keys": ["system_prompt", "llm_provider", ...]}``.

        Keys in ``{system_prompt, description}`` are removed from the ``meta``
        scope; all other keys are removed from the ``llm`` scope. When any
        provider-shaping key is reset, the adapter's child LLM is rebuilt so
        the change takes effect without a restart.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        registry = state.registry
        if registry is None:
            return _storage_not_ready()
        config_manager = registry.config
        config_storage = state.config_storage
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        keys = body.get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            return JSONResponse(
                {"error": "'keys' must be an array of strings"}, status_code=400,
            )

        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)

        locked = set(_locked_llm_fields_for_tool(tool))
        if locked:
            blocked = [k for k in keys if k in locked]
            if blocked:
                return JSONResponse(
                    {
                        "error": (
                            f"Cannot reset locked LLM parameter(s) "
                            f"{blocked} for tool '{tool_id}'."
                        )
                    },
                    status_code=400,
                )

        storage = config_manager.storage
        from app.storage.tool_storage import SCOPE_LLM, SCOPE_META
        for key in keys:
            scope = SCOPE_META if key in _META_KEYS else SCOPE_LLM
            try:
                storage.delete_config(
                    profile=profile, tool_id=tool_id, scope=scope, key=key,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"Failed to delete {scope}.{key} for tool '{tool_id}'"
                )

        # If a provider-shaping key was reset, rebuild the child LLM using
        # the remaining DB values (falling back to the profile's model group).
        if any(k in ("llm_provider", "llm_model", "reasoning_effort") for k in keys) \
                and hasattr(tool, "update_runtime_config"):
            try:
                merged = config_manager.get_llm_params(tool_id, profile)
                provider_name = merged.get("llm_provider") or None
                model_name = merged.get("llm_model") or None
                if not model_name:
                    group_value = BaseConfig.get_model_group("low", profile=profile)
                    if group_value:
                        parts = group_value.split("/", 1)
                        if not provider_name:
                            provider_name = parts[0]
                        model_name = parts[1] if len(parts) > 1 else parts[0]
                if not provider_name:
                    provider_name = BaseConfig.get_default_provider(profile=profile)
                if model_name:
                    new_llm = create_llm_provider(
                        provider_name=provider_name,
                        model_name=model_name,
                        config_storage=config_storage,
                        profile=profile,
                        default_reasoning_effort=merged.get("reasoning_effort"),
                    )
                    tool.update_runtime_config(llm=new_llm)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"Failed to rebuild LLM for tool '{tool_id}' after reset"
                )

        # Push cleared system_prompt / description straight to the running
        # adapter so in-flight changes take effect immediately.
        if hasattr(tool, "update_runtime_config"):
            if "system_prompt" in keys:
                tool.update_runtime_config(system_prompt="")
            if "description" in keys:
                tool.update_runtime_config(description="")

        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    async def handle_set_enabled(request: Request) -> JSONResponse:
        """Enable / disable an A2A or MCP tool for the current profile.

        On enable, if the tool is currently a stub (lazy-init placeholder or
        previous connection failure), schedule a background connect attempt.
        The HTTP response returns immediately so the UI toggle stays snappy;
        the next ``GET /api/tools`` will reflect the new connection state.

        For built-in tools that declare ``requires_feature`` in their
        TOOL_CONFIG, the handler pre-flights the dependency before
        flipping the flag — if the feature isn't installed, returns
        HTTP 409 with ``FeatureNotInstalled`` so the frontend can drive
        the install through ``POST /api/features/install`` (SSE) and
        then retry the toggle. The pre-flight is skipped on disable.
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
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        enabled = body.get("enabled")
        if enabled is None:
            return JSONResponse({"error": "'enabled' field is required"}, status_code=400)

        # Feature pre-flight (built-in tools only, enable direction only).
        # When the user re-enables a tool whose pip extras were never
        # installed, fail loudly with 409 instead of silently flipping a
        # flag that leaves the tool in MissingDependency forever. The
        # frontend catches the 409, runs the install via SSE, and
        # re-issues the PUT.
        if bool(enabled):
            from app.tools.builtin import required_feature_for_tool_id
            from app.features.manifest import FEATURES, is_installed

            feature_key = required_feature_for_tool_id(tool_id)
            if feature_key and feature_key in FEATURES:
                if not is_installed(feature_key):
                    feature = FEATURES[feature_key]
                    return JSONResponse(
                        {
                            "error": "FeatureNotInstalled",
                            "tool_id": tool_id,
                            "feature_key": feature_key,
                            "extras": list(feature.extras),
                            "requires_restart_after_install": feature.requires_restart,
                            "message": (
                                f"The '{tool_id}' tool requires the "
                                f"'{feature_key}' feature, which is not "
                                "installed. Install it via "
                                "POST /api/features/install before enabling."
                            ),
                        },
                        status_code=409,
                    )

        try:
            registry.set_profile_tool_enabled(profile, tool_id, bool(enabled))
        except KeyError:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        connect_persisted_tool = state.connect_persisted_tool
        if bool(enabled) and connect_persisted_tool is not None:
            tool = registry.get(tool_id)
            if tool is not None and getattr(tool, "is_stub", False):
                asyncio.create_task(connect_persisted_tool(tool_id))

        publish_settings_state_changed(profile)
        return JSONResponse({"success": True, "enabled": bool(enabled)})

    async def handle_register_long_running_app(request: Request) -> JSONResponse:
        """Spawn a skill's declared ``long_running_app`` and persist it as autostart.

        Reads ``long_running_app.command`` from the skill's ``SKILL.md``
        metadata, inserts a row into ``autostart_processes``, then immediately
        spawns the command via :func:`spawn_from_autostart` so the user sees
        the new process in the registry.

        Body: ``{"force": bool}`` -- when true, bypass the duplicate check.
        Returns ``{process_id, autostart_id, command, working_dir}``.
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
        try:
            body = await request.json()
        except Exception:
            body = {}
        force = bool(body.get("force") or False)

        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        if tool.tool_type is not ToolType.SKILL:
            return JSONResponse(
                {"error": "Tool is not a skill"}, status_code=400,
            )

        lra = _long_running_app_for_tool(tool)
        if lra is None:
            return JSONResponse(
                {"error": "Skill has no long_running_app metadata"},
                status_code=400,
            )
        # Run the command from the skill's directory so relative paths like
        # ``scripts/event_listener.py`` resolve correctly.
        working_dir = str(getattr(tool, "info").dir_path)
        # Normalize forward-slash relative paths in the command to the OS's
        # native separator so the same SKILL.md works on POSIX and Windows.
        command = normalize_command_paths(lra["command"], working_dir)

        storage = get_autostart_storage()
        duplicate = storage.find_duplicate(profile, command)
        if duplicate and not force:
            return JSONResponse(
                {
                    "error": "duplicate",
                    "message": "A registration with the same command already exists.",
                    "existing": duplicate,
                },
                status_code=409,
            )

        row = storage.insert(
            profile=profile,
            command=command,
            working_dir=working_dir,
            is_pty=False,
        )

        process_id, error = await spawn_from_autostart(row)
        if process_id is None:
            # The user clicked Register, so we should *only* persist the
            # registration when the command actually starts. Roll back the
            # row so the failed command doesn't show up on the Processes
            # page or get retried at the next boot.
            storage.delete(row["id"], profile)
            publish_process_list_changed(profile)
            return JSONResponse(
                {
                    "error": "spawn_failed",
                    "message": error or "Failed to spawn process",
                },
                status_code=500,
            )

        publish_process_list_changed(profile)
        return JSONResponse({
            "process_id": process_id,
            "autostart_id": row["id"],
            "command": command,
            "working_dir": working_dir,
        })

    return [
        Route("/api/tools", handle_list_tools, methods=["GET"]),
        Route("/api/tools/{tool_id}", handle_get_tool, methods=["GET"]),
        Route("/api/tools/{tool_id}/variables", handle_set_variables, methods=["PUT"]),
        Route("/api/tools/{tool_id}/arguments", handle_set_arguments, methods=["PUT"]),
        Route("/api/tools/{tool_id}/llm", handle_set_llm_params, methods=["PUT"]),
        Route("/api/tools/{tool_id}/llm", handle_reset_llm_params, methods=["DELETE"]),
        Route("/api/tools/{tool_id}/enabled", handle_set_enabled, methods=["PUT"]),
        Route(
            "/api/tools/{tool_id}/long-running-app/register",
            handle_register_long_running_app,
            methods=["POST"],
        ),
    ]


# ── helpers ────────────────────────────────────────────────────────────────


_META_KEYS: frozenset[str] = frozenset({"system_prompt", "description"})


def _long_running_app_for_tool(tool) -> dict | None:
    """Return a skill's declared ``long_running_app`` metadata, or None.

    Pulled from the skill's ``SKILL.md`` frontmatter via ``SkillInfo.metadata``
    (the inner ``metadata: { … }`` block stored by the scanner). Only returns
    the block when it has a non-empty ``command`` string -- malformed entries
    are silently ignored so the rest of the tool listing keeps working.
    """
    if tool is None or tool.tool_type is not ToolType.SKILL:
        return None
    info = getattr(tool, "info", None)
    if info is None:
        return None
    raw = info.metadata.get("long_running_app") if isinstance(info.metadata, dict) else None
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    out: dict = {"command": command.strip()}
    description = raw.get("description")
    if isinstance(description, str) and description:
        out["description"] = description
    return out


def _llm_defaults_for_tool(tool) -> dict:
    """Return the code-level ``llm_parameters`` defaults for a built-in tool.

    For skills / MCP / A2A / intrinsic tools, returns ``{}`` — those don't ship
    code-level defaults via ``TOOL_CONFIG``.
    """
    if tool is None:
        return {}
    if tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
        schema = get_builtin_tool_config(tool.config_name)
        return dict(schema.get("tool", {}).get("llm_parameters") or {})
    return {}


def _locked_llm_fields_for_tool(tool) -> list[str]:
    """Return ``locked_llm_fields`` declared by a built-in tool's TOOL_CONFIG.

    These keys cannot be overridden via the API or the Settings UI. Returns
    an empty list for tools without that declaration (the common case).
    """
    if tool is None:
        return []
    if tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
        schema = get_builtin_tool_config(tool.config_name)
        raw = schema.get("tool", {}).get("locked_llm_fields") or []
        if isinstance(raw, list):
            return [str(k) for k in raw if isinstance(k, str)]
    return []


def _schema_for_tool(tool) -> dict:
    """Return the static config schema for a built-in tool or skill, or {} for others.

    Skills declare their environment variables via ``metadata.environment_variables``
    in ``SKILL.md`` (a list of variable names). They are surfaced through the same
    ``required_config`` shape used by built-in tools so the existing UI/save flow
    works without further branching.
    """
    if tool is None:
        return {}
    if tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
        return get_builtin_tool_config(tool.config_name)
    if tool.tool_type is ToolType.SKILL:
        names = getattr(tool, "environment_variables", []) or []
        if not names:
            return {}
        required = {
            name: {"description": name, "type": "string", "secret": False}
            for name in names
        }
        return {"tool": {"required_config": required}}
    return {}


def _escape_env_value(value: str) -> str:
    """Quote a value for safe inclusion in a ``.env`` file.

    Wraps in double quotes and escapes embedded ``"`` / ``\\`` if the value
    contains whitespace, ``#``, or quotes; otherwise returns it as-is.
    """
    if value == "":
        return ""
    needs_quoting = any(ch in value for ch in (" ", "\t", "\n", "\r", "#", '"', "'", "\\", "$"))
    if not needs_quoting:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_skill_env_file(tool, config_manager, profile: str) -> None:
    """Mirror persisted skill variables to ``{skill_dir}/scripts/.env``.

    Overwrites the file so deletions in the DB also disappear from disk. Only
    variables declared in the skill's ``environment_variables`` are written —
    stray DB rows are ignored.
    """
    try:
        scripts_dir = tool.info.dir_path / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        env_path = scripts_dir / ".env"
        all_vars = config_manager.get_variables(
            tool.tool_id, profile, include_secrets=True,
        )
        declared = set(getattr(tool, "environment_variables", []) or [])
        lines = [
            f"{k}={_escape_env_value(str(v))}"
            for k, v in all_vars.items()
            if k in declared and v != ""
        ]
        body = "\n".join(lines) + ("\n" if lines else "")
        env_path.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.exception(
            f"Failed to write .env for skill '{getattr(tool, 'tool_id', '?')}'"
        )


def _is_tool_configured(tool, snapshot: dict) -> bool:
    """Return True if all required variables are populated for this tool.

    Fields that declare a ``default`` value are considered satisfied even
    when the user has not explicitly set them.
    """
    schema = _schema_for_tool(tool)
    required = schema.get("tool", {}).get("required_config", {})
    if not required:
        return True
    have = snapshot.get("variables", {})
    return all(
        have.get(k) or field_spec.get("default") is not None
        for k, field_spec in required.items()
    )
