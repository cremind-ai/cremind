"""Unit tests for the MCPConnection lifecycle and transport adapter.

Tests cover:
- Initial uninitialized state and connection guard assertions
- stdio transport initialization, environment variable merging, and server metadata extraction
- HTTP transport initialization, header propagation, and Bearer token parsing
- Tool execution with real MCP SDK models, argument propagation, and timeout enforcement
- HTTP reconnection lifecycle, old session closing, and header/token refresh
- Safe exit stack cleanup under normal conditions, standard exceptions, and asyncio.CancelledError
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client.stdio import StdioServerParameters
from mcp.types import (
    CallToolResult,
    Implementation,
    InitializeResult,
    ListToolsResult,
    ServerCapabilities,
    TextContent,
    Tool,
)

from app.tools.mcp.mcp_connection import MCPConnection


def _create_mock_init_result(name: str = "test-mcp-server", instructions: str = "Test instructions") -> InitializeResult:
    """Create an official MCP InitializeResult model for tests."""
    return InitializeResult(
        protocolVersion="2024-11-05",
        capabilities=ServerCapabilities(),
        serverInfo=Implementation(name=name, version="1.0.0"),
        instructions=instructions,
    )


def _create_mock_tools_result(tool_names: list[str]) -> ListToolsResult:
    """Create an official MCP ListToolsResult model with Tool instances."""
    return ListToolsResult(
        tools=[
            Tool(
                name=t_name,
                description=f"Description for {t_name}",
                inputSchema={"type": "object", "properties": {"input": {"type": "string"}}},
            )
            for t_name in tool_names
        ]
    )


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


def test_reconnect_http_guards():
    # 1. Transport not HTTP
    conn = MCPConnection()
    conn._transport_type = "stdio"
    conn._url = "some_command"
    with pytest.raises(RuntimeError, match="reconnect_http only works for HTTP transport connections"):
        asyncio.run(conn.reconnect_http())

    # 2. HTTP transport but no URL
    conn._transport_type = "http"
    conn._url = ""
    with pytest.raises(RuntimeError, match="reconnect_http only works for HTTP transport connections"):
        asyncio.run(conn.reconnect_http())


def test_connect_stdio_success_and_metadata():
    async def _test():
        conn = MCPConnection()

        mock_session = AsyncMock()
        mock_session.initialize.return_value = _create_mock_init_result(name="stdio-fs", instructions="File server")
        mock_session.list_tools.return_value = _create_mock_tools_result(["read_file", "write_file"])

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


def test_connect_stdio_environment_merging_and_isolation(monkeypatch):
    """Verify that stdio environment merges system os.environ and custom env, and handles overrides."""
    async def _test():
        monkeypatch.setenv("SYSTEM_KEY", "system_default")
        monkeypatch.setenv("OVERRIDE_KEY", "system_override_me")

        conn = MCPConnection()

        mock_session = AsyncMock()
        mock_session.initialize.return_value = _create_mock_init_result()
        mock_session.list_tools.return_value = _create_mock_tools_result([])

        mock_stdio_cm = AsyncMock()
        mock_stdio_cm.__aenter__.return_value = (AsyncMock(), AsyncMock())
        mock_stdio_cm.__aexit__.return_value = None

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__.return_value = mock_session
        mock_session_cm.__aexit__.return_value = None

        with (
            patch("app.tools.mcp.mcp_connection.stdio_client", return_value=mock_stdio_cm) as mock_stdio_factory,
            patch("app.tools.mcp.mcp_connection.ClientSession", return_value=mock_session_cm),
        ):
            # Test 1: Merging and override
            await conn.connect_stdio(
                "python",
                ["script.py"],
                env={"CUSTOM_KEY": "custom_val", "OVERRIDE_KEY": "overridden"},
            )

            mock_stdio_factory.assert_called_once()
            called_params = mock_stdio_factory.call_args[0][0]
            assert isinstance(called_params, StdioServerParameters)
            assert called_params.command == "python"
            assert called_params.args == ["script.py"]
            assert called_params.env["SYSTEM_KEY"] == "system_default"
            assert called_params.env["CUSTOM_KEY"] == "custom_val"
            assert called_params.env["OVERRIDE_KEY"] == "overridden"

            mock_stdio_factory.reset_mock()

            # Test 2: env=None preserves system environment without failure
            conn2 = MCPConnection()
            await conn2.connect_stdio("python", ["script.py"], env=None)
            called_params2 = mock_stdio_factory.call_args[0][0]
            assert called_params2.env["SYSTEM_KEY"] == "system_default"
            assert "CUSTOM_KEY" not in called_params2.env

    asyncio.run(_test())


def test_connect_http_header_propagation_and_token_extraction():
    """Verify streamablehttp_client receives exact headers and Bearer token parsing behaves correctly."""
    async def _test():
        conn = MCPConnection()

        mock_session = AsyncMock()
        mock_session.initialize.return_value = _create_mock_init_result(name="remote-mcp", instructions="Cloud tools")
        mock_session.list_tools.return_value = _create_mock_tools_result(["search_web"])

        mock_http_cm = AsyncMock()
        mock_http_cm.__aenter__.return_value = (AsyncMock(), AsyncMock(), None)
        mock_http_cm.__aexit__.return_value = None

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__.return_value = mock_session
        mock_session_cm.__aexit__.return_value = None

        headers = {
            "Authorization": "Bearer secret_api_key_12345",
            "X-Custom-Header": "custom-value",
        }

        with (
            patch("app.tools.mcp.mcp_connection.streamablehttp_client", return_value=mock_http_cm) as mock_http_factory,
            patch("app.tools.mcp.mcp_connection.ClientSession", return_value=mock_session_cm),
        ):
            await conn.connect_http("https://api.example.com/mcp", headers=headers)

            # Ensure streamablehttp_client received the exact URL and headers passed
            mock_http_factory.assert_called_once_with("https://api.example.com/mcp", headers=headers)

        assert conn.transport_type == "http"
        assert conn.url == "https://api.example.com/mcp"
        assert conn.server_name == "remote-mcp"
        assert conn.current_auth_token == "secret_api_key_12345"
        assert len(conn.get_tools()) == 1
        assert conn.get_tools()[0].name == "search_web"

        # Verify non-Bearer or missing headers
        conn_no_auth = MCPConnection()
        conn_no_auth._current_headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        assert conn_no_auth.current_auth_token is None

        conn_no_headers = MCPConnection()
        assert conn_no_headers.current_auth_token is None

    asyncio.run(_test())


def test_reconnect_http_success_and_state_refresh():
    """Verify reconnect_http closes old resources, resets state, and connects with new headers."""
    async def _test():
        conn = MCPConnection()

        old_session = AsyncMock()
        old_session.initialize.return_value = _create_mock_init_result(name="server-v1")
        old_session.list_tools.return_value = _create_mock_tools_result(["tool_v1"])

        new_session = AsyncMock()
        new_session.initialize.return_value = _create_mock_init_result(name="server-v2")
        new_session.list_tools.return_value = _create_mock_tools_result(["tool_v1", "tool_v2"])

        mock_http_cm = AsyncMock()
        mock_http_cm.__aenter__.return_value = (AsyncMock(), AsyncMock(), None)
        mock_http_cm.__aexit__.return_value = None

        mock_session_cm_1 = AsyncMock()
        mock_session_cm_1.__aenter__.return_value = old_session
        mock_session_cm_1.__aexit__.return_value = None

        mock_session_cm_2 = AsyncMock()
        mock_session_cm_2.__aenter__.return_value = new_session
        mock_session_cm_2.__aexit__.return_value = None

        with (
            patch("app.tools.mcp.mcp_connection.streamablehttp_client", return_value=mock_http_cm) as mock_http_factory,
            patch("app.tools.mcp.mcp_connection.ClientSession", side_effect=[mock_session_cm_1, mock_session_cm_2]),
        ):
            # Initial connection
            await conn.connect_http(
                "https://api.example.com/mcp",
                headers={"Authorization": "Bearer token_v1"},
            )
            assert conn.current_auth_token == "token_v1"
            assert [t.name for t in conn.get_tools()] == ["tool_v1"]

            initial_stack = conn.exit_stack
            with patch.object(initial_stack, "aclose", wraps=initial_stack.aclose) as spy_aclose:
                # Reconnect with new credentials
                await conn.reconnect_http(headers={"Authorization": "Bearer token_v2"})

                # Verify old exit stack was closed
                spy_aclose.assert_awaited_once()

            # Verify new state
            assert conn.exit_stack is not initial_stack
            assert conn.session == new_session
            assert conn.current_auth_token == "token_v2"
            assert [t.name for t in conn.get_tools()] == ["tool_v1", "tool_v2"]
            assert mock_http_factory.call_count == 2
            mock_http_factory.assert_called_with(
                "https://api.example.com/mcp",
                headers={"Authorization": "Bearer token_v2"},
            )

    asyncio.run(_test())


def test_call_tool_execution_with_real_mcp_result_and_timeout():
    """Verify tool execution returning CallToolResult and timeout enforcement."""
    async def _test():
        conn = MCPConnection()
        mock_session = AsyncMock()

        # Real MCP CallToolResult model
        expected_result = CallToolResult(
            content=[TextContent(type="text", text="Operation completed successfully")],
            isError=False,
        )
        mock_session.call_tool.return_value = expected_result
        conn.session = mock_session

        # 1. Successful execution
        result = await conn.call_tool("search_web", {"q": "test"})
        assert result == expected_result
        assert result.content[0].text == "Operation completed successfully"
        mock_session.call_tool.assert_awaited_once_with("search_web", {"q": "test"})

        # 2. Timeout simulation
        async def slow_call(*args, **kwargs):
            await asyncio.sleep(0.5)
            return expected_result

        mock_session.call_tool = slow_call
        with pytest.raises(asyncio.TimeoutError):
            await conn.call_tool("search_web", {"q": "slow"}, timeout=0.05)

    asyncio.run(_test())


def test_cleanup_normal_and_exception_handling():
    """Verify cleanup succeeds under normal conditions and catches standard exceptions."""
    async def _test():
        conn = MCPConnection()
        mock_stack = AsyncMock()
        mock_stack.aclose.side_effect = Exception("Simulated transport socket drop")
        conn.exit_stack = mock_stack

        # Normal exception is caught and suppressed
        await conn.cleanup()
        mock_stack.aclose.assert_awaited_once()

    asyncio.run(_test())


def test_cleanup_safe_handling_of_cancelled_error():
    """Verify cleanup safely catches and suppresses asyncio.CancelledError (BaseException)."""
    async def _test():
        conn = MCPConnection()
        mock_stack = AsyncMock()
        mock_stack.aclose.side_effect = asyncio.CancelledError("Connection cleanup cancelled by task abort")
        conn.exit_stack = mock_stack

        # asyncio.CancelledError must be caught by cleanup without bubbling up
        await conn.cleanup()
        mock_stack.aclose.assert_awaited_once()

    asyncio.run(_test())
