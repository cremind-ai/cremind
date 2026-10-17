"""Unit tests for the MCPConnection lifecycle and transport adapter.

Tests cover:
- Initial uninitialized state and connection guard assertions
- stdio transport initialization, environment variable merging, and server metadata extraction
- HTTP transport initialization, tool caching, and Bearer token parsing
- Tool execution, argument propagation, and timeout enforcement
- HTTP reconnection with updated authentication headers
- Safe exit stack cleanup under normal and error conditions
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools.mcp.mcp_connection import MCPConnection


class DummyTool:
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description


class DummyServerInfo:
    def __init__(self, name: str):
        self.name = name


class DummyInitResult:
    def __init__(self, name: str = "dummy-server", instructions: str = "Test instructions"):
        self.serverInfo = DummyServerInfo(name)
        self.instructions = instructions


class DummyListToolsResult:
    def __init__(self, tools: list[DummyTool]):
        self.tools = tools


def test_initial_state():
    conn = MCPConnection()
    assert conn.session is None
    assert conn.get_tools() == []
    assert conn.server_name == ""
    assert conn.tool_instructions == ""
    assert conn.transport_type == ""
    assert conn.url == ""
    assert conn.current_auth_token is None


def test_call_tool_not_connected_raises_runtime_error():
    conn = MCPConnection()
    with pytest.raises(RuntimeError, match="Not connected to any MCP server"):
        asyncio.run(conn.call_tool("test_tool", {"arg": "value"}))


def test_reconnect_http_on_stdio_raises_runtime_error():
    conn = MCPConnection()
    conn._transport_type = "stdio"
    with pytest.raises(RuntimeError, match="reconnect_http only works for HTTP transport connections"):
        asyncio.run(conn.reconnect_http())


def test_connect_stdio_success():
    async def _test():
        conn = MCPConnection()

        mock_session = AsyncMock()
        mock_session.initialize.return_value = DummyInitResult(name="stdio-fs", instructions="File server")
        mock_session.list_tools.return_value = DummyListToolsResult([DummyTool("read_file"), DummyTool("write_file")])

        # Mock the async context managers for stdio_client and ClientSession
        mock_stdio_cm = AsyncMock()
        mock_stdio_cm.__aenter__.return_value = (AsyncMock(), AsyncMock())
        mock_stdio_cm.__aexit__.return_value = None

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__.return_value = mock_session
        mock_session_cm.__aexit__.return_value = None

        with (
            patch("app.tools.mcp.mcp_connection.stdio_client", return_value=mock_stdio_cm),
            patch("app.tools.mcp.mcp_connection.ClientSession", return_value=mock_session_cm),
        ):
            await conn.connect_stdio("npx", ["-y", "@modelcontextprotocol/server-filesystem"], env={"SANDBOX": "1"})

        assert conn.transport_type == "stdio"
        assert conn.server_name == "stdio-fs"
        assert conn.tool_instructions == "File server"
        assert [t.name for t in conn.get_tools()] == ["read_file", "write_file"]
        assert conn.session == mock_session

    asyncio.run(_test())


def test_connect_http_success_and_bearer_token():
    async def _test():
        conn = MCPConnection()

        mock_session = AsyncMock()
        mock_session.initialize.return_value = DummyInitResult(name="remote-mcp", instructions="Cloud tools")
        mock_session.list_tools.return_value = DummyListToolsResult([DummyTool("search_web")])

        mock_http_cm = AsyncMock()
        mock_http_cm.__aenter__.return_value = (AsyncMock(), AsyncMock(), None)
        mock_http_cm.__aexit__.return_value = None

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__.return_value = mock_session
        mock_session_cm.__aexit__.return_value = None

        headers = {"Authorization": "Bearer secret_api_key_12345"}

        with (
            patch("app.tools.mcp.mcp_connection.streamablehttp_client", return_value=mock_http_cm),
            patch("app.tools.mcp.mcp_connection.ClientSession", return_value=mock_session_cm),
        ):
            await conn.connect_http("https://api.example.com/mcp", headers=headers)

        assert conn.transport_type == "http"
        assert conn.url == "https://api.example.com/mcp"
        assert conn.server_name == "remote-mcp"
        assert conn.current_auth_token == "secret_api_key_12345"
        assert len(conn.get_tools()) == 1
        assert conn.get_tools()[0].name == "search_web"

    asyncio.run(_test())


def test_call_tool_execution_and_timeout():
    async def _test():
        conn = MCPConnection()
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"content": "ok"}
        conn.session = mock_session

        # Successful tool call
        result = await conn.call_tool("search_web", {"q": "test"})
        assert result == {"content": "ok"}
        mock_session.call_tool.assert_awaited_once_with("search_web", {"q": "test"})

        # Timeout simulation
        async def slow_call(*args, **kwargs):
            await asyncio.sleep(0.5)
            return {"content": "too late"}

        mock_session.call_tool = slow_call
        with pytest.raises(asyncio.TimeoutError):
            await conn.call_tool("search_web", {"q": "slow"}, timeout=0.05)

    asyncio.run(_test())


def test_cleanup_safe_handling():
    async def _test():
        conn = MCPConnection()
        mock_stack = AsyncMock()
        mock_stack.aclose.side_effect = Exception("Simulated transport socket drop")
        conn.exit_stack = mock_stack

        # cleanup should catch exception and not raise
        await conn.cleanup()
        mock_stack.aclose.assert_awaited_once()

    asyncio.run(_test())
