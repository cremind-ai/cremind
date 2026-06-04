"""MCP (Model Context Protocol) integration.

Provides MCP server connection management, OAuth, and a registry-facing
:class:`MCPServerTool` wrapper that exposes one MCP server (HTTP or stdio) as
a unified :class:`Tool`.
"""

from app.tools.mcp.mcp_agent_adapter import MCPAgentAdapter
from app.tools.mcp.mcp_auth import MCPOAuthClient
from app.tools.mcp.mcp_connection import MCPConnection
from app.tools.mcp.mcp_remote_shim import MCPRemoteConnectionShim
from app.tools.mcp.tool import (
    MCPServerTool,
    build_http_mcp_tool,
    build_mcp_stub,
    build_stdio_mcp_tool,
    derive_server_name,
    make_stdio_url,
)

__all__ = [
    "MCPConnection",
    "MCPAgentAdapter",
    "MCPOAuthClient",
    "MCPRemoteConnectionShim",
    "MCPServerTool",
    "build_http_mcp_tool",
    "build_stdio_mcp_tool",
    "build_mcp_stub",
    "derive_server_name",
    "make_stdio_url",
]
