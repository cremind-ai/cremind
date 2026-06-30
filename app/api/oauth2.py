"""Unified OAuth2 callback for MCP servers and built-in tools.

State format (base64-encoded):
    ``{random_token}:{profile}:{source}:{tool_id}``
"""

from __future__ import annotations

import base64

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.tools import ToolRegistry, ToolType
from app.utils import logger


def get_oauth2_routes(
    *,
    registry: ToolRegistry,
    pending_return_urls: dict,
) -> list[Route]:

    async def handle_oauth2_callback(request: Request) -> HTMLResponse | RedirectResponse:
        state = request.query_params.get("state")
        logger.info(f"[auth] OAuth2 callback received: state={'present' if state else 'absent'}")
        if not state:
            return HTMLResponse("<h1>Error: Missing state parameter</h1>", status_code=400)

        profile = None
        source = "api"
        tool_id = None
        try:
            decoded_state = base64.urlsafe_b64decode(state).decode()
            parts = decoded_state.split(":")
            if len(parts) >= 2:
                profile = parts[1]
            if len(parts) >= 3:
                source = parts[2]
            if len(parts) >= 4:
                tool_id = ":".join(parts[3:])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[auth] OAuth2 state decode failed: {e}")
            return HTMLResponse("<h1>Error: Invalid state parameter</h1>", status_code=400)

        if not profile:
            return HTMLResponse(
                "<h1>Error: Profile could not be determined from callback state</h1>",
                status_code=400,
            )
        if not tool_id:
            return HTMLResponse(
                "<h1>Error: Tool id could not be determined from callback state</h1>",
                status_code=400,
            )

        code = request.query_params.get("code")
        if not code:
            error = request.query_params.get("error", "Unknown error")
            error_desc = request.query_params.get("error_description", "")
            return HTMLResponse(
                f"<h1>Authentication Failed</h1><p>Error: {error}</p><p>{error_desc}</p>",
                status_code=400,
            )

        tool = registry.get(tool_id)
        if tool is None:
            logger.warning(f"[auth] OAuth2 callback for unknown tool_id={tool_id} profile={profile}")
            return HTMLResponse(
                f"<h1>Error: Tool '{tool_id}' not found</h1>", status_code=404,
            )

        oauth_client = _get_oauth_client(tool, profile)
        if oauth_client is None:
            logger.warning(f"[auth] OAuth2 callback but tool_id={tool_id} has no OAuth client (profile={profile})")
            return HTMLResponse(
                f"<h1>Error: Tool '{tool_id}' does not support OAuth</h1>",
                status_code=400,
            )

        logger.info(f"[auth] OAuth2 exchange starting: tool_id={tool_id} profile={profile} source={source}")
        redirect_uri = f"{BaseConfig.APP_URL}/oauth2/callback"
        success = await oauth_client.handle_oauth_callback(
            code, redirect_uri, state, profile,
        )
        logger.info(f"[auth] OAuth2 exchange {'succeeded' if success else 'failed'}: tool_id={tool_id} profile={profile}")

        if success and tool.tool_type is ToolType.MCP:
            adapter = getattr(tool, "adapter", None)
            if adapter is not None and hasattr(adapter, "_ensure_auth"):
                try:
                    await adapter._ensure_auth(profile)
                    logger.info(f"[auth] MCP server connected after auth: '{tool.name}'")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[auth] Failed to connect MCP server after auth: {e}")

        if success:
            if source == "chat":
                return HTMLResponse(
                    "<h1>Authentication successful!</h1>"
                    "<p>You can close this tab and continue your conversation.</p>"
                    "<script>setTimeout(function(){ window.close(); }, 2000);</script>"
                )
            return_url = pending_return_urls.pop((tool_id, profile), None)
            if return_url:
                sep = "&" if "?" in return_url else "?"
                return RedirectResponse(url=f"{return_url}{sep}agents=open")
            return HTMLResponse(
                "<h1>Authentication Successful</h1>"
                "<p>You can close this window and return to the app.</p>"
                "<script>setTimeout(function(){window.close()},3000);</script>"
            )
        return HTMLResponse(
            f"<h1>Authentication Failed</h1>"
            f"<p>Failed to exchange authorization code for tool '{tool_id}' "
            f"(profile: {profile}).</p>",
            status_code=500,
        )

    return [
        Route(path="/oauth2/callback", methods=["GET"], endpoint=handle_oauth2_callback),
    ]


def _get_oauth_client(tool, profile: str):
    """Return the OAuth client for ``tool``, or ``None`` if not OAuth-capable."""
    adapter = getattr(tool, "adapter", None)
    if adapter is None:
        return None
    return getattr(adapter, "_mcp_auth", None)
