"""Tool management API.

Replaces the old per-type ``/api/tools`` + ``/api/agents`` + ``/api/mcp-servers``
endpoints with a unified surface keyed by ``tool_id``. Built-in and skill
configuration is also exposed here.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth_or_setup_mode
from app.config.settings import BaseConfig
from app.events.settings_state_bus import publish_settings_state_changed
from app.lib.llm.factory import create_llm_provider
from app.runtime import BootedState
from app.skills.env_file import write_skill_env_file
from app.skills.sync import is_builtin_skill_dir, list_builtin_skill_catalog
from app.storage import get_autostart_storage
from app.tools import ToolType
from app.tools.builtin import (
    BuiltInToolGroup,
    get_builtin_tool_config,
    get_builtin_variable_options_hook,
    list_builtin_tool_catalog,
    refresh_builtin_tool_oauth,
)
from app.tools.builtin.exec_shell import publish_process_list_changed
from app.tools.builtin.exec_shell_autostart import (
    normalize_command_paths,
    spawn_from_autostart,
    teardown_processes_for_dir,
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
        # Deferred-storage window: no registry yet (first-run setup, pre-Apply).
        # Return the static built-in tool catalog AND the built-in skill catalog
        # so the wizard's Tools step can render tools and skills together before
        # the user clicks "Apply" (which materialises storage + the registry and
        # seeds this profile's skills). Skills are absent from the registry here
        # because they are profile-owned and no profile exists yet.
        if registry is None:
            return JSONResponse(
                {"tools": list_builtin_tool_catalog() + list_builtin_skill_catalog()}
            )
        config_manager = registry.config
        profile = _profile_from_request(request)
        if not profile and config_storage is not None and config_storage.is_setup_complete():
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        rows = registry.visible_for_profile(profile)
        # Keep this view consistent with what the reasoning agent is actually
        # given: the ``image_understanding`` tool is available iff the model that
        # would run it can see images (the dedicated vision model when the
        # Specialized Vision Model feature is on, otherwise the main model). Hidden
        # only when the feature is off AND the main model is text-only.
        mgr = getattr(state, "model_group_mgr", None)
        if mgr is not None and not mgr.image_understanding_available(profile):
            rows = [r for r in rows if r["tool_id"] != "image_understanding"]
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
                # Optional pip-extras feature key (built-in tools only).
                # Drives the Setup Wizard "Installs: cremind[…]" hint and
                # the post-setup enable-toggle install dialog.
                "requires_feature": schema.get("tool", {}).get("requires_feature"),
                # Built-in tools only: when True the Settings toggle is locked
                # on (the API rejects disable) — see ToolConfig.locked.
                "toggle_locked": bool(schema.get("tool", {}).get("locked", False)),
            })
            if hasattr(tool, "connection_error") and getattr(tool, "connection_error"):
                row["connection_error"] = tool.connection_error
                row["is_stub"] = True
            else:
                row["is_stub"] = bool(getattr(tool, "is_stub", False))
            # Multi-leaf tools (built-in groups, MCP servers) expose a per-
            # sub-tool toggle section; the UI lazy-loads leaves only for these.
            try:
                row["supports_leaf_toggle"] = len(tool.skills) > 1
            except Exception:  # noqa: BLE001
                row["supports_leaf_toggle"] = False
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
        allow_unknown = bool(body.get("allow_unknown"))

        tool = registry.get(tool_id)
        schema = _schema_for_tool(tool) if tool else {}
        required_config = schema.get("tool", {}).get("required_config", {})

        # Validate up front so a bad value never partially writes: reject any
        # value that isn't a member of its field's declared ``enum``. Booleans
        # are normalized first so a native JSON ``true`` matches an enum of
        # ``["true", "false"]``.
        normalized: dict[str, str] = {}
        for key, value in variables.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            value = str(value)
            enum = required_config.get(key, {}).get("enum")
            if enum and value not in enum:
                return JSONResponse(
                    {
                        "error": (
                            f"'{value}' is not a valid value for '{key}'. "
                            f"Allowed values: {', '.join(map(str, enum))}."
                        ),
                        "key": key,
                        "allowed": list(enum),
                    },
                    status_code=400,
                )
            normalized[key] = value

        # For variables with a live option list (``dynamic_options``, e.g.
        # CLAUDE_CODE_MODEL / CLAUDE_CODE_PERMISSION_MODE), reject a value the
        # resolved list doesn't actually offer — the list is fetched live (models
        # include aliases; permission modes come from the installed SDK). Skipped
        # when the caller opts out (``allow_unknown``: the Web UI's free-form
        # field, the CLI ``--force`` flag) or when the list can't be resolved
        # (offline / no credential / SDK not installed), so legitimate custom
        # values and offline use still work. Runs before the write loop so a bad
        # value never partially persists.
        dynamic_keys = [
            k for k, v in normalized.items()
            if v and required_config.get(k, {}).get("dynamic_options")
        ]
        if (
            dynamic_keys
            and not allow_unknown
            and tool is not None
            and tool.tool_type is ToolType.BUILTIN
            and isinstance(tool, BuiltInToolGroup)
        ):
            hook = get_builtin_variable_options_hook(tool.config_name)
            if hook is not None:
                merged = {
                    **config_manager.get_variables(tool_id, profile, include_secrets=True),
                    **normalized,
                }
                try:
                    options_result = await hook(variables=merged, profile=profile, refresh=False)
                except Exception:  # noqa: BLE001 — never block a write on the hook
                    logger.debug(
                        "variable-options check failed for tool '%s'", tool_id, exc_info=True
                    )
                    options_result = {}
                for key in dynamic_keys:
                    info = options_result.get(key) or {}
                    valid = {
                        o["id"]
                        for o in (info.get("options") or [])
                        if isinstance(o, dict) and o.get("id")
                    }
                    if valid and normalized[key] not in valid:
                        return JSONResponse(
                            {
                                "error": (
                                    f"'{normalized[key]}' is not one of the available "
                                    f"values for '{key}'. Valid values: "
                                    f"{', '.join(sorted(valid))}. Re-run with a valid one "
                                    f"(see `cremind tools options {tool_id}`), or pass --force "
                                    f"to set it anyway."
                                ),
                                "key": key,
                                "allowed": sorted(valid),
                            },
                            status_code=400,
                        )

        for key, value in normalized.items():
            field_spec = required_config.get(key, {})
            is_secret = bool(field_spec.get("secret") or _is_secret_var_name(key))
            config_manager.set_variable(
                tool_id, profile, key, value, is_secret=is_secret,
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
        """Update Tool Arguments (JSON-Schema parameter values) for a tool.

        Counterpart to :func:`handle_set_variables` for the ``arg`` scope. The
        matching CLI command (``cremind tools set-args``) and its client wrapper
        already shipped, but this route was never registered — so ``set-args``
        used to 404. Values persist per profile and are injected into each
        matching tool call by the reasoning agent on its next step.
        """
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

        # Unlike variables, refuse an unknown tool: ``tool_configs.tool_id`` is a
        # foreign key, so writing rows for a nonexistent tool would fail on
        # Postgres. (handle_set_variables skips this check — a known looseness we
        # do not copy here.)
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse(
                {"error": f"Tool '{tool_id}' not found"}, status_code=404,
            )

        # Validate keys and enum membership against the tool's arguments schema
        # (populated for built-ins now that register_builtin_tools wires it).
        arg_schema = getattr(tool, "arguments_schema", None) or {}
        properties = arg_schema.get("properties", {}) if isinstance(arg_schema, dict) else {}
        for key, value in arguments.items():
            if properties and key not in properties:
                return JSONResponse(
                    {
                        "error": f"'{key}' is not a valid argument for '{tool_id}'.",
                        "key": key,
                        "valid_keys": list(properties.keys()),
                    },
                    status_code=400,
                )
            enum = properties.get(key, {}).get("enum") if properties else None
            if enum and value not in enum:
                return JSONResponse(
                    {
                        "error": (
                            f"'{value}' is not a valid value for argument '{key}'. "
                            f"Allowed values: {', '.join(map(str, enum))}."
                        ),
                        "key": key,
                        "allowed": list(enum),
                    },
                    status_code=400,
                )

        config_manager.set_arguments(tool_id, profile, arguments)
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

        # Self-heal: tear down any listener already running for this skill dir
        # (and drop its stale autostart rows) before spawning a fresh one. A
        # single-instance listener takes an exclusive lock on
        # ``scripts/.listener.lock``; if a prior one is still alive — including
        # an orphan left behind on Windows after Stop/Unregister killed only the
        # shell leader — the new process would exit immediately ("another
        # listener is already running"). Killing the tree first makes Register
        # idempotent: it always restarts cleanly.
        await teardown_processes_for_dir(Path(working_dir), profile=profile)

        storage = get_autostart_storage()
        duplicate = storage.find_duplicate(profile, command, working_dir=working_dir)
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

    async def handle_list_leaves(request: Request) -> JSONResponse:
        """List a tool's sub-tools ("leaves") with their per-profile enabled state.

        Returns ``{supports_leaf_toggle, disconnected, leaves: [{leaf_name,
        name, description, enabled}]}``. Built-in groups list statically; MCP
        servers list live and return an empty list (``disconnected=true``) when
        the connection is down.
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
            return JSONResponse(registry.leaves_for_profile(profile, tool_id))
        except KeyError:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)

    async def handle_set_leaves(request: Request) -> JSONResponse:
        """Enable/disable one or more sub-tools ("leaves") of a tool.

        Body: ``{"leaves": {"<leaf_name>": <bool>, ...}}``. A single key is a
        per-row toggle; many keys drive "Enable all" / "Disable all". Unknown
        leaf names are rejected when the tool exposes a live sub-tool list;
        when that list is empty (e.g. a disconnected MCP server) the write is
        accepted so a persisted choice survives a reconnect.
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
        leaves = body.get("leaves")
        if not isinstance(leaves, dict) or not leaves:
            return JSONResponse(
                {"error": "'leaves' must be a non-empty object of {leaf_name: bool}"},
                status_code=400,
            )

        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        # Validate against the live sub-tool list when available.
        try:
            known = {s.id for s in tool.skills}
        except Exception:  # noqa: BLE001
            known = set()
        if known:
            unknown = [name for name in leaves if name not in known]
            if unknown:
                return JSONResponse(
                    {"error": f"Unknown sub-tool(s): {', '.join(sorted(unknown))}"},
                    status_code=400,
                )

        try:
            for leaf, enabled in leaves.items():
                registry.set_profile_tool_leaf_enabled(
                    profile, tool_id, str(leaf), bool(enabled),
                )
        except KeyError:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        # Deliberately NOT publishing a settings-state change: the reasoning
        # agent re-reads disabled leaves from the DB every step (no in-memory
        # cache to invalidate), and an SSE refresh would collapse the expanded
        # Settings card the user is toggling sub-tools in.
        return JSONResponse({"success": True})

    async def handle_get_variable_options(request: Request) -> JSONResponse:
        """Live option lists for a tool's ``dynamic_options`` variables.

        For a built-in tool that exports a ``get_variable_options`` hook (e.g.
        ``claude_code`` for its ``CLAUDE_CODE_MODEL`` field), return the current
        option list — the Settings dropdown and ``cremind tools options`` render
        it. Query ``?refresh=1`` bypasses the server-side cache. Tools without a
        hook return ``{"variables": {}}`` (200) so callers degrade to a text
        input. Values are advisory: they are NOT enforced on write.
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
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)

        hook = None
        if tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
            hook = get_builtin_variable_options_hook(tool.config_name)
        if hook is None:
            return JSONResponse({"tool_id": tool_id, "variables": {}})

        # Current values incl. secrets — the hook resolves account credentials
        # from them (e.g. CLAUDE_CODE_API_KEY). Only non-secret labels/options
        # are returned to the caller.
        variables = registry.config.get_variables(tool_id, profile, include_secrets=True)
        refresh = request.query_params.get("refresh") in ("1", "true")
        try:
            result = await hook(variables=variables, profile=profile, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 — hooks shouldn't raise; belt-and-braces
            logger.exception("variable-options hook failed for tool '%s'", tool_id)
            return JSONResponse(
                {"error": f"Failed to resolve options: {exc}"}, status_code=500
            )
        return JSONResponse({"tool_id": tool_id, "variables": result})

    return [
        Route("/api/tools", handle_list_tools, methods=["GET"]),
        Route("/api/tools/{tool_id}", handle_get_tool, methods=["GET"]),
        Route("/api/tools/{tool_id}/variables", handle_set_variables, methods=["PUT"]),
        Route(
            "/api/tools/{tool_id}/variable-options",
            handle_get_variable_options,
            methods=["GET"],
        ),
        Route("/api/tools/{tool_id}/arguments", handle_set_arguments, methods=["PUT"]),
        Route("/api/tools/{tool_id}/enabled", handle_set_enabled, methods=["PUT"]),
        Route("/api/tools/{tool_id}/leaves", handle_list_leaves, methods=["GET"]),
        Route("/api/tools/{tool_id}/leaves", handle_set_leaves, methods=["PUT"]),
        Route(
            "/api/tools/{tool_id}/long-running-app/register",
            handle_register_long_running_app,
            methods=["POST"],
        ),
    ]


# ── helpers ────────────────────────────────────────────────────────────────


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


def _is_secret_var_name(name: str) -> bool:
    """Heuristic: does this variable name look like a secret to mask in the UI?

    Kept in sync with the secret auto-detection in ``handle_set_variables``.
    """
    lowered = name.lower()
    return "secret" in lowered or "key" in lowered or "password" in lowered


def _schema_for_tool(tool) -> dict:
    """Return the static config schema for a built-in tool or skill, or {} for others.

    Skills declare their environment variables via ``metadata.environment_variables``
    in ``SKILL.md`` — a list of per-variable objects (see
    :attr:`app.skills.tool.SkillTool.environment_variables`). Each object's
    ``required`` / ``secret`` / ``type`` / ``enum`` / ``default`` / ``description``
    metadata is mapped onto the same ``required_config`` field shape used by
    built-in tools so the existing UI/save flow renders it without branching.

    Gating: a variable marked ``required: true`` blocks "needs config" until set;
    optional variables carry a ``default`` (the author's value, or ``""``) so they
    stay editable but never block — the skill's own code supplies the real runtime
    fallback (``os.environ.get(name, default)``).
    """
    if tool is None:
        return {}
    if tool.tool_type is ToolType.BUILTIN and isinstance(tool, BuiltInToolGroup):
        return get_builtin_tool_config(tool.config_name)
    if tool.tool_type is ToolType.SKILL:
        specs = getattr(tool, "environment_variables", []) or []
        if not specs:
            return {}
        required_config: dict[str, dict] = {}
        for spec in specs:
            name = spec["name"]
            is_required = bool(spec.get("required"))
            secret = spec.get("secret")
            field: dict = {
                "description": spec.get("description") or name,
                "type": spec.get("type") or "string",
                "secret": secret if secret is not None else _is_secret_var_name(name),
                "required": is_required,
            }
            if spec.get("enum"):
                field["enum"] = spec["enum"]
            default = spec.get("default")
            if is_required:
                # Gating keys off ``required``; only surface a default the
                # author explicitly provided (purely a UI hint/placeholder).
                if default is not None:
                    field["default"] = default
            else:
                # Optional: always carry a default (real value or "") so the
                # field is pre-fillable and clearly non-blocking.
                field["default"] = default if default is not None else ""
            required_config[name] = field
        return {"tool": {"required_config": required_config}}
    return {}


def _write_skill_env_file(tool, config_manager, profile: str) -> None:
    """Mirror persisted skill variables to ``{skill_dir}/scripts/.env``.

    Thin wrapper over :func:`app.skills.env_file.write_skill_env_file` that pulls
    the declared vars + persisted values for this tool/profile.
    """
    all_vars = config_manager.get_variables(
        tool.tool_id, profile, include_secrets=True,
    )
    declared = getattr(tool, "environment_variable_names", []) or []
    write_skill_env_file(tool.info.dir_path / "scripts", declared, all_vars)


def _is_tool_configured(tool, snapshot: dict) -> bool:
    """Return True if all required variables are populated for this tool.

    A field is satisfied when the user has set a value, OR the field is
    explicitly optional (``required: False`` — skill env vars), OR it declares
    a ``default`` (built-in tools, whose field specs carry no ``required`` flag).
    """
    schema = _schema_for_tool(tool)
    required = schema.get("tool", {}).get("required_config", {})
    if not required:
        return True
    have = snapshot.get("variables", {})

    def _satisfied(key: str, field_spec: dict) -> bool:
        if have.get(key):
            return True
        required_flag = field_spec.get("required")
        if required_flag is True:
            return False
        if required_flag is False:
            return True
        return field_spec.get("default") is not None

    return all(_satisfied(k, spec) for k, spec in required.items())
