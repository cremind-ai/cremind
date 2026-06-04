"""MCP client connection management.

Manages MCP ClientSession lifecycle for both stdio and HTTP (streamable-http) transports.
"""

import asyncio
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool as MCPTool

from app.utils.logger import logger


class MCPConnection:
    """Manages an MCP client session with support for stdio and HTTP transports."""

    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self._tools: List[MCPTool] = []
        self._server_name: str = ""
        self._server_instructions: str = ""
        self._transport_type: str = ""  # "stdio" or "http"
        self._url: str = ""
        self._current_headers: Optional[Dict[str, str]] = None

    async def connect_stdio(self, command: str, args: List[str], env: Optional[Dict[str, str]] = None):
        """Connect to an MCP server over JSON-RPC stdio transport.

        Args:
            command: The executable command (e.g., "python")
            args: Command arguments (e.g., ["app/mcp/stdio/weather.py"])
            env: Optional environment variables (merged with os.environ)
        """
        logger.info(f"Connecting to MCP server via stdio: {command} {' '.join(args)}")

        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=merged_env,
        )

        stdio_transport = await self.exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        read, write = stdio_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(read, write)
        )

        init_result = await self.session.initialize()
        self._transport_type = "stdio"

        # Cache tools and server name
        response = await self.session.list_tools()
        self._tools = response.tools

        # Get server name and instructions from initialize result
        self._extract_server_info(init_result)

        logger.info(
            f"Connected to stdio MCP server '{self._server_name}' "
            f"with tools: {[t.name for t in self._tools]}"
        )

    async def connect_http(self, url: str, headers: Optional[Dict[str, str]] = None):
        """Connect to an MCP server over streamable-http transport.

        Args:
            url: The HTTP endpoint URL of the MCP server
            headers: Optional HTTP headers (e.g., Authorization)
        """
        logger.info(f"Connecting to MCP server via HTTP: {url}")
        self._url = url
        self._current_headers = headers

        http_transport = await self.exit_stack.enter_async_context(
            streamablehttp_client(url, headers=headers)
        )
        read, write, _ = http_transport
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(read, write)
        )

        init_result = await self.session.initialize()
        self._transport_type = "http"

        # Cache tools and server name
        response = await self.session.list_tools()
        self._tools = response.tools

        # Get server name and instructions from initialize result
        self._extract_server_info(init_result)

        logger.info(
            f"Connected to HTTP MCP server '{self._server_name}' "
            f"with tools: {[t.name for t in self._tools]}"
        )

    async def refresh_tools(self) -> List[MCPTool]:
        """Refresh the cached tool list from the server."""
        if not self.session:
            raise RuntimeError("Not connected to any MCP server")
        response = await self.session.list_tools()
        self._tools = response.tools
        return self._tools

    def get_tools(self) -> List[MCPTool]:
        """Return the cached list of MCP tools."""
        return self._tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        """Execute a tool call on the connected MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments as a dictionary
            timeout: Optional timeout in seconds. None means no timeout.

        Returns:
            The tool result from the MCP server

        Raises:
            asyncio.TimeoutError: If the call exceeds the timeout
            RuntimeError: If not connected
        """
        if not self.session:
            raise RuntimeError("Not connected to any MCP server")
        if timeout is not None:
            return await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments),
                timeout=timeout,
            )
        return await self.session.call_tool(tool_name, arguments)

    def _extract_server_info(self, init_result):
        """Extract server name and instructions from the MCP InitializeResult."""
        if init_result:
            server_info = getattr(init_result, 'serverInfo', None)
            if server_info:
                self._server_name = getattr(server_info, 'name', '') or ''
            self._server_instructions = getattr(init_result, 'instructions', '') or ''

    @property
    def server_name(self) -> str:
        """Return the server name from MCP server info."""
        return self._server_name

    @property
    def tool_instructions(self) -> str:
        """Return the server instructions from MCP initialize result."""
        return self._server_instructions

    @property
    def transport_type(self) -> str:
        """Return the transport type ('stdio' or 'http')."""
        return self._transport_type

    @property
    def url(self) -> str:
        """Return the connection URL (for HTTP) or command identifier (for stdio)."""
        return self._url

    async def reconnect_http(self, headers: Optional[Dict[str, str]] = None):
        """Reconnect to an HTTP MCP server with new headers (e.g., after authentication).

        Args:
            headers: New HTTP headers to use (e.g., Authorization bearer token)
        """
        if self._transport_type != "http" or not self._url:
            raise RuntimeError("reconnect_http only works for HTTP transport connections")

        url = self._url
        await self.exit_stack.aclose()
        self.exit_stack = AsyncExitStack()
        self.session = None
        self._tools = []

        await self.connect_http(url, headers=headers)

    @property
    def current_auth_token(self) -> Optional[str]:
        """Return the current auth token from headers, if any."""
        if not hasattr(self, '_current_headers') or not self._current_headers:
            return None
        auth = self._current_headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    async def cleanup(self):
        """Clean up all resources."""
        try:
            await self.exit_stack.aclose()
        except (Exception, asyncio.CancelledError) as e:
            logger.warning(f"Error during MCP connection cleanup: {e}")
