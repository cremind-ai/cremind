"""MCP-server registration API.

Endpoints (kept compatible with the existing frontend; tool_id replaces
agent_name in path params):

- ``GET    /api/agents?profile=...``                     list mcp tools
- ``POST   /api/agents``                                 register a new MCP server
- ``DELETE /api/agents/{tool_id}``                       unregister a tool
- ``PUT    /api/agents/{tool_id}/enabled``               toggle visibility for a profile
- ``POST   /api/agents/{tool_id}/reconnect``             retry a failed connection
- ``GET    /api/agents/{tool_id}/auth-url``              OAuth: get authorization URL
- ``POST   /api/agents/{tool_id}/unlink``                drop the OAuth token for this profile
- ``GET    /api/agents/{tool_id}/config``                MCP-server LLM/system_prompt config
- ``PUT    /api/agents/{tool_id}/config``                update LLM/system_prompt
"""

from __future__ import annotations

import asyncio
import inspect
import json
import urllib.parse

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth_or_setup_mode
from app.config.settings import BaseConfig
from app.events.settings_state_bus import publish_settings_state_changed
from app.lib.llm.factory import create_llm_provider
from app.tools import ToolRegistry, ToolType
from app.tools.mcp import (
    MCPServerTool,
    build_http_mcp_tool,
    build_mcp_stub,
    build_stdio_mcp_tool,
    derive_server_name,
)
from app.utils.logger import logger


# ── helpers ────────────────────────────────────────────────────────────────


def _parse_mcp_json_config(raw):
    """Parse VS Code-style MCP server JSON config into a normalized dict."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
    else:
        data = raw

    if not isinstance(data, dict):
        raise ValueError("JSON config must be an object")

    if "servers" in data and isinstance(data["servers"], dict) and len(data) == 1:
        data = data["servers"]

    if "type" in data or "command" in data or "url" in data:
        return _validate_transport_config(data, name=None)

    if not data:
        raise ValueError("JSON config is empty")
    if len(data) > 1:
        raise ValueError("JSON config must contain exactly one server entry")

    server_name, server_config = next(iter(data.items()))
    if not isinstance(server_config, dict):
        raise ValueError(f"Server config for '{server_name}' must be an object")
    return _validate_transport_config(server_config, name=server_name)


def _validate_transport_config(config: dict, name):
    transport_type = config.get("type", "").lower()

    if transport_type == "stdio":
        command = config.get("command")
        if not command or not isinstance(command, str):
            raise ValueError("stdio config requires a 'command' string")
        args = config.get("args", [])
        if not isinstance(args, list):
            raise ValueError("'args' must be an array")
        env = config.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError("'env' must be an object")
        return {
            "name": name, "transport_type": "stdio",
            "command": command, "args": args, "env": env,
        }

    if transport_type in ("http", "sse"):
        url = config.get("url")
        if not url:
            raise ValueError(f"{transport_type} config requires a 'url' string")
        return {
            "name": name, "transport_type": "http",
            "url": url, "headers": config.get("headers"),
        }

    if transport_type == "":
        if config.get("url"):
            return {
                "name": name, "transport_type": "http",
                "url": config["url"], "headers": config.get("headers"),
            }
        if config.get("command"):
            return _validate_transport_config({**config, "type": "stdio"}, name=name)
        raise ValueError("Config must specify 'type', 'url', or 'command'")

    raise ValueError(f"Unsupported transport type: '{transport_type}'")


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _serialize_tool(tool, profile: str) -> dict:
    """Render a tool as a JSON dict for the agents list endpoint."""
    auth_status = "not_supported"
    expiration_info = None
    show_authenticate = False
    show_unlink = False
    badge_class = "badge-secondary"
    status_text = "Agent does not support authentication"

    if getattr(tool, "connection_error", None):
        badge_class = "badge-danger"
        status_text = f"Connection error: {tool.connection_error}"

    description = tool.description or "No description available"
    if len(description) > 100:
        description = description[:97] + "..."

    return {
        "tool_id": tool.tool_id,
        "name": tool.name,
        "encoded_name": urllib.parse.quote(tool.name),
        "description": description,
        "url": getattr(tool, "url", None),
        "badge_class": badge_class,
        "status_text": status_text,
        "expiration_info": expiration_info,
        "show_authenticate": show_authenticate,
        "show_unlink": show_unlink,
        "arguments_schema": tool.arguments_schema,
        "agent_type": tool.tool_type.value,
        "owner_profile": getattr(tool, "owner_profile", None),
        "connection_error": getattr(tool, "connection_error", None),
        "is_stub": getattr(tool, "is_stub", False),
    }


def _persist_llm_scope(config_manager, tool_id: str, profile: str, body: dict) -> None:
    for key in ("llm_provider", "llm_model", "reasoning_effort", "full_reasoning"):
        if body.get(key) is not None:
            config_manager.set_llm_param(tool_id, profile, key, body[key])


def _get_oauth_client(tool, profile: str):
    """Return the OAuth client used by ``tool`` for ``profile``, or None."""
    # MCP servers and built-in tools both carry their OAuth client on the adapter.
    adapter = getattr(tool, "adapter", None)
    if adapter is not None:
        return getattr(adapter, "_mcp_auth", None)
    return None


# ── route factory ──────────────────────────────────────────────────────────


def get_agent_routes(
    *,
    registry: ToolRegistry,
    pending_return_urls: dict,
    mcp_llm_factory=None,
    config_storage=None,
    connect_persisted_tool=None,
) -> list[Route]:

    config_manager = registry.config

    def _resolve_mcp_llm(profile: str = "admin"):
        if mcp_llm_factory is not None:
            try:
                return mcp_llm_factory(profile=profile)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"mcp_llm_factory raised: {e}")
        return None

    def _bind_llm_factory(tool: MCPServerTool) -> None:
        """Attach a per-tool LLM factory so execute() picks up config changes."""
        if mcp_llm_factory is not None:
            tid = tool.tool_id
            tool._llm_factory = lambda profile: mcp_llm_factory(tid, profile)

    async def handle_list_agents(request: Request) -> JSONResponse:
        """Open during first-run setup (no JWT, no profile) so the
        wizard can render the (likely empty) agents list; gated
        post-setup."""
        denied = require_auth_or_setup_mode(request, config_storage)
        if denied is not None:
            return denied
        profile = _profile_from_request(request)
        if not profile and config_storage is not None and config_storage.is_setup_complete():
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        enabled_per_profile = (
            registry.storage.list_profile_tools(profile) if profile else {}
        )
        agents = []
        for tool in registry.all_tools():
            if tool.tool_type is not ToolType.MCP:
                continue
            row = _serialize_tool(tool, profile)
            row["enabled"] = enabled_per_profile.get(tool.tool_id, False)
            agents.append(row)
        return JSONResponse({"agents": agents})

    async def handle_add_agent(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        url = body.get("url")
        agent_type = body.get("type", "mcp")
        profile = _profile_from_request(request)
        json_config = body.get("json_config")

        if agent_type != "mcp":
            return JSONResponse(
                {"error": f"Unsupported type: {agent_type}. Only 'mcp' is supported."},
                status_code=400,
            )
        if not url and not json_config:
            return JSONResponse({"error": "URL is required"}, status_code=400)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        # MCP path
        llm = _resolve_mcp_llm(profile)
        if llm is None:
            return JSONResponse(
                {"error": "MCP support is not configured"}, status_code=500,
            )

        llm_provider = body.get("llm_provider")
        llm_model = body.get("llm_model")
        reasoning_effort = body.get("reasoning_effort")
        system_prompt = body.get("system_prompt")
        agent_description = body.get("description")

        if llm_provider or llm_model:
            try:
                llm = create_llm_provider(
                    provider_name=llm_provider or BaseConfig.get_default_provider(),
                    model_name=llm_model,
                    config_storage=config_storage,
                    profile=profile,
                    default_reasoning_effort=reasoning_effort,
                )
            except ValueError as e:
                return JSONResponse({"error": f"Invalid LLM config: {e}"}, status_code=400)

        if json_config:
            try:
                parsed = _parse_mcp_json_config(json_config)
            except ValueError as e:
                return JSONResponse({"error": f"Invalid MCP JSON config: {e}"}, status_code=400)
            transport_type = parsed["transport_type"]
            if transport_type == "stdio":
                try:
                    tool = await build_stdio_mcp_tool(
                        command=parsed["command"], args=parsed["args"],
                        env=parsed.get("env"), llm=llm, owner_profile=profile,
                        system_prompt=system_prompt, description=agent_description,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Failed to start stdio MCP server: {e}")
                    return JSONResponse(
                        {"error": f"Failed to start stdio MCP server: {e}"},
                        status_code=400,
                    )
                await registry.register_mcp(
                    tool, source=tool.url, owner_profile=profile,
                    extra={k: v for k, v in {
                        "transport_type": "stdio",
                        "command": parsed["command"], "args": parsed["args"],
                        "env": parsed.get("env"),
                    }.items() if v is not None},
                )
                _bind_llm_factory(tool)
                _persist_llm_scope(config_manager, tool.tool_id, profile, body)
                publish_settings_state_changed(profile)
                return JSONResponse(
                    {"success": True, "agent": _serialize_tool(tool, profile)},
                    status_code=201,
                )
            url = parsed["url"]

        if not url:
            return JSONResponse({"error": "URL is required"}, status_code=400)

        try:
            tool = await build_http_mcp_tool(
                url=url, llm=llm, owner_profile=profile,
                system_prompt=system_prompt, description=agent_description,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to add MCP server at {url}: {e}")
            return JSONResponse({"error": str(e)}, status_code=400)
        await registry.register_mcp(tool, source=tool.url, owner_profile=profile)
        _bind_llm_factory(tool)
        _persist_llm_scope(config_manager, tool.tool_id, profile, body)
        if system_prompt:
            config_manager.set_meta(tool.tool_id, profile, "system_prompt", system_prompt)
        if agent_description:
            config_manager.set_meta(tool.tool_id, profile, "description", agent_description)
        publish_settings_state_changed(profile)
        return JSONResponse(
            {"success": True, "agent": _serialize_tool(tool, profile)},
            status_code=201,
        )

    async def handle_remove_agent(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        if tool.tool_type is not ToolType.MCP:
            return JSONResponse(
                {"error": "Only MCP tools can be removed via this endpoint"},
                status_code=400,
            )
        if isinstance(tool, MCPServerTool):
            try:
                connection = getattr(tool.adapter, "_connection", None)
                if connection and hasattr(connection, "cleanup"):
                    await connection.cleanup()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"MCP connection cleanup for '{tool_id}' failed; proceeding with unregister: {e}")
        await registry.unregister(tool_id)
        publish_settings_state_changed(_profile_from_request(request))
        return JSONResponse({"success": True, "tool_id": tool_id})

    async def handle_toggle_enabled(request: Request) -> JSONResponse:
        """Enable / disable an A2A or MCP tool for a profile.

        On enable, if the tool is currently a stub (lazy-init placeholder or
        previous connection failure), schedule a background connect attempt
        so the tool flips from stub → live without user intervention. The
        response returns immediately; the next ``GET /api/agents`` will
        reflect the new connection state.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        enabled = body.get("enabled")
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        if enabled is None:
            return JSONResponse({"error": "'enabled' field is required"}, status_code=400)
        try:
            registry.set_profile_tool_enabled(profile, tool_id, bool(enabled))
        except KeyError:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        if bool(enabled) and connect_persisted_tool is not None:
            tool = registry.get(tool_id)
            if tool is not None and getattr(tool, "is_stub", False):
                asyncio.create_task(connect_persisted_tool(tool_id))

        publish_settings_state_changed(profile)
        return JSONResponse({"success": True, "enabled": bool(enabled)})

    async def handle_reconnect(request: Request) -> JSONResponse:
        """Retry a failed connection for an A2A/MCP stub tool.

        Unlike the lazy-connect fire-and-forget path, this endpoint awaits
        the connection attempt and reports success / failure synchronously.
        Uses the same ``connect_persisted_tool`` helper, which swaps the
        stub in place via ``registry.replace_tool`` — preserving every
        profile's ``profile_tools.enabled`` state.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        if tool.tool_type is not ToolType.MCP:
            return JSONResponse(
                {"error": f"Tool '{tool_id}' is not an MCP tool"}, status_code=400,
            )
        if not getattr(tool, "is_stub", False):
            return JSONResponse(
                {"error": f"Tool '{tool_id}' is already connected"}, status_code=400,
            )
        if connect_persisted_tool is None:
            return JSONResponse(
                {"error": "Lazy-connect not configured"}, status_code=500,
            )
        success, error = await connect_persisted_tool(tool_id)
        if not success:
            logger.error(f"Reconnect failed for {tool_id}: {error}")
            return JSONResponse({"error": error or "Reconnect failed"}, status_code=400)
        publish_settings_state_changed(_profile_from_request(request))
        return JSONResponse({"success": True, "tool_id": tool_id})

    async def handle_get_auth_url(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        return_url = request.query_params.get("return_url")
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        oauth_client = _get_oauth_client(tool, profile)
        if oauth_client is None:
            return JSONResponse(
                {"error": f"Tool '{tool_id}' does not support OAuth"}, status_code=400,
            )
        redirect_uri = f"{BaseConfig.APP_URL}/oauth2/callback"
        auth_url = oauth_client.get_auth_url(redirect_uri, profile, source="api")
        if inspect.isawaitable(auth_url):
            auth_url = await auth_url
        if not auth_url:
            return JSONResponse(
                {"error": f"Tool '{tool_id}' does not expose an OAuth flow"},
                status_code=400,
            )
        if return_url:
            pending_return_urls[(tool_id, profile)] = return_url
        return JSONResponse({"auth_url": auth_url, "tool_id": tool_id})

    async def handle_unlink(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        oauth_client = _get_oauth_client(tool, profile)
        if oauth_client is None:
            return JSONResponse(
                {"error": f"Tool '{tool_id}' does not support OAuth"}, status_code=400,
            )
        success = oauth_client.unlink_token(profile)
        if success:
            publish_settings_state_changed(profile)
        return JSONResponse({"success": bool(success)})

    async def handle_get_agent_config(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        if tool.tool_type not in (ToolType.MCP, ToolType.BUILTIN):
            return JSONResponse(
                {"error": f"Tool '{tool_id}' does not support agent configuration"}, status_code=400,
            )
        llm_params = config_manager.get_llm_params(tool_id, profile)
        meta = config_manager.get_meta(tool_id, profile)
        return JSONResponse({
            "config": {
                "url": getattr(tool, "url", None),
                "llm_provider": llm_params.get("llm_provider"),
                "llm_model": llm_params.get("llm_model"),
                "reasoning_effort": llm_params.get("reasoning_effort"),
                "full_reasoning": llm_params.get("full_reasoning"),
                "system_prompt": meta.get("system_prompt"),
                "description": meta.get("description"),
            }
        })

    async def handle_update_agent_config(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        tool_id = request.path_params["tool_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        tool = registry.get(tool_id)
        if tool is None:
            return JSONResponse({"error": f"Tool '{tool_id}' not found"}, status_code=404)
        if tool.tool_type not in (ToolType.MCP, ToolType.BUILTIN):
            return JSONResponse(
                {"error": f"Tool '{tool_id}' does not support agent configuration"}, status_code=400,
            )

        _MISSING = object()
        llm_provider = body.get("llm_provider", _MISSING)
        llm_model = body.get("llm_model", _MISSING)
        reasoning_effort = body.get("reasoning_effort", _MISSING)
        system_prompt = body.get("system_prompt", _MISSING)
        agent_description = body.get("description", _MISSING)
        full_reasoning = body.get("full_reasoning", _MISSING)

        llm_keys_present = any(
            v is not _MISSING for v in (llm_provider, llm_model, reasoning_effort)
        )
        if llm_keys_present:
            try:
                prov = llm_provider if llm_provider is not _MISSING else None
                model = llm_model if llm_model is not _MISSING else None
                effort = reasoning_effort if reasoning_effort is not _MISSING else None

                # When no per-tool override, fall back to the single configured
                # model (same source as startup in server.py _builtin_llm_factory).
                if not model:
                    group_value = BaseConfig.get_model_group("high", profile=profile)
                    if group_value:
                        parts = group_value.split("/", 1)
                        if not prov:
                            prov = parts[0]
                        model = parts[1] if len(parts) > 1 else parts[0]

                if not prov:
                    prov = BaseConfig.get_default_provider(profile=profile)

                if model:
                    new_llm = create_llm_provider(
                        provider_name=prov or BaseConfig.get_default_provider(),
                        model_name=model,
                        config_storage=config_storage,
                        profile=profile,
                        default_reasoning_effort=effort,
                    )
                    tool.update_runtime_config(llm=new_llm)
                else:
                    logger.warning(
                        f"No model resolved for tool '{tool_id}'; skipping LLM rebuild"
                    )
            except ValueError as e:
                return JSONResponse({"error": f"Invalid LLM: {e}"}, status_code=400)

        tool.update_runtime_config(
            system_prompt=system_prompt if system_prompt is not _MISSING else None,
            description=agent_description if agent_description is not _MISSING else None,
            full_reasoning=full_reasoning if full_reasoning is not _MISSING else None,
        )

        for key, value in [
            ("llm_provider", llm_provider),
            ("llm_model", llm_model),
            ("reasoning_effort", reasoning_effort),
            ("full_reasoning", full_reasoning),
        ]:
            if value is not _MISSING:
                config_manager.set_llm_param(tool_id, profile, key, value)
        for key, value in [
            ("system_prompt", system_prompt),
            ("description", agent_description),
        ]:
            if value is not _MISSING:
                config_manager.set_meta(tool_id, profile, key, value)

        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    return [
        Route("/api/agents", handle_list_agents, methods=["GET"]),
        Route("/api/agents", handle_add_agent, methods=["POST"]),
        Route("/api/agents/{tool_id}", handle_remove_agent, methods=["DELETE"]),
        Route("/api/agents/{tool_id}/enabled", handle_toggle_enabled, methods=["PUT"]),
        Route("/api/agents/{tool_id}/reconnect", handle_reconnect, methods=["POST"]),
        Route("/api/agents/{tool_id}/auth-url", handle_get_auth_url, methods=["GET"]),
        Route("/api/agents/{tool_id}/unlink", handle_unlink, methods=["POST"]),
        Route("/api/agents/{tool_id}/config", handle_get_agent_config, methods=["GET"]),
        Route("/api/agents/{tool_id}/config", handle_update_agent_config, methods=["PUT"]),
    ]
