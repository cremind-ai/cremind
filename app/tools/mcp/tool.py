"""MCP server wrapped as a unified :class:`Tool`.

An MCP server can talk MCP-protocol tools, but the reasoning agent never sees
that protocol -- it sees a single registry tool whose ``execute()`` returns
observation parts. The child LLM here selects which MCP-protocol tool(s) to
call; this is the "low" model group (or per-server override).
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from a2a.types import AgentCard

from app.lib.llm.base import LLMProvider
from app.tools.base import (
    FunctionSpec,
    Tool,
    make_leaf_name,
    ToolErrorEvent,
    ToolEvent,
    ToolResultEvent,
    ToolSkill,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.tools.mcp.mcp_agent_adapter import MCPAgentAdapter
from app.tools.mcp.mcp_auth import MCPOAuthClient
from app.tools.mcp.mcp_connection import MCPConnection
from app.utils.event_parser import parse_agent_events
from app.utils.logger import logger


class MCPServerTool(Tool):
    """Wraps an MCP server (HTTP or stdio) as a registry :class:`Tool`."""

    tool_type = ToolType.MCP

    def __init__(
        self,
        *,
        url: str,
        owner_profile: Optional[str],
        adapter: MCPAgentAdapter,
        connection_error: Optional[str] = None,
        extra: Optional[dict] = None,
    ):
        super().__init__()
        self._url = url
        self._owner_profile = owner_profile
        self._adapter = adapter
        self._connection_error = connection_error
        self._extra = extra or {}

    # ── identity ────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return self._url

    @property
    def owner_profile(self) -> Optional[str]:
        return self._owner_profile

    @property
    def name(self) -> str:
        return self._adapter.name or self._extra.get("server_name") or self._url

    @property
    def description(self) -> str:
        return self._adapter.description or "MCP server"

    @property
    def adapter(self) -> MCPAgentAdapter:
        return self._adapter

    @property
    def connection_error(self) -> Optional[str]:
        return self._connection_error

    @property
    def is_stub(self) -> bool:
        return self._connection_error is not None

    @property
    def skills(self) -> List[ToolSkill]:
        return [
            ToolSkill(id=s.id, name=s.name, description=s.description)
            for s in self._adapter.get_skills()
        ]

    # ── native function calling ─────────────────────────────────────────

    def leaf_function_specs(
        self,
        *,
        context_id: str,
        profile: str,
        query: str = "",
        arguments=None,
    ) -> List[FunctionSpec]:
        """Expose each MCP tool as a native function, namespaced ``<tool_id>__<leaf>``."""
        if self._connection_error:
            return []
        specs = self._adapter.build_specs()
        out: List[FunctionSpec] = []
        for s in specs:
            fn = s.get("function") or {}
            leaf = fn.get("name") or ""
            if not leaf:
                continue
            exposed = make_leaf_name(self.tool_id, leaf)
            schema = {"type": "function", "function": {**fn, "name": exposed}}
            out.append(FunctionSpec(name=exposed, leaf_name=leaf, schema=schema))
        return out

    async def execute_leaf(
        self,
        *,
        leaf_name: str,
        args: Dict[str, Any],
        context_id: str,
        profile: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
    ) -> AsyncGenerator[ToolEvent, None]:
        if self._connection_error:
            yield ToolErrorEvent(
                message=(
                    f"MCP server '{self.name}' is unavailable: {self._connection_error}. "
                    "Please reconnect or disable this tool."
                )
            )
            return

        yield ToolThinkingEvent()

        events: list = []
        try:
            async for ev in self._adapter.request(
                context_id=context_id, profile=profile,
                decided_calls=[{"name": leaf_name, "arguments": args}],
            ):
                events.append(ev)
                yield ToolStatusEvent(raw=ev)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"MCP tool '{self.name}' failed")
            yield ToolErrorEvent(message=str(e))
            return

        observation_text, token_usage, observation_parts = parse_agent_events(events)
        yield ToolResultEvent(
            observation_text=observation_text,
            observation_parts=observation_parts,
            token_usage=token_usage or {},
        )

    # ── runtime updates ─────────────────────────────────────────────────

    def update_runtime_config(
        self,
        *,
        llm: Optional[LLMProvider] = None,
        description: Optional[str] = None,
    ) -> None:
        self._adapter.update_config(llm=llm, description=description)

    # ── execution ───────────────────────────────────────────────────────

    async def execute(
        self,
        *,
        query: str,
        context_id: str,
        profile: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
    ) -> AsyncGenerator[ToolEvent, None]:
        if self._connection_error:
            yield ToolErrorEvent(
                message=(
                    f"MCP server '{self.name}' is unavailable: {self._connection_error}. "
                    "Please reconnect or disable this tool."
                )
            )
            return

        yield ToolThinkingEvent()

        events: list = []

        try:
            async for ev in self._adapter.request(
                context_id=context_id, profile=profile,
            ):
                events.append(ev)
                yield ToolStatusEvent(raw=ev)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"MCP tool '{self.name}' failed")
            yield ToolErrorEvent(message=str(e))
            return

        observation_text, token_usage, observation_parts = parse_agent_events(events)
        yield ToolResultEvent(
            observation_text=observation_text,
            observation_parts=observation_parts,
            token_usage=token_usage or {},
        )


# ── factory functions ─────────────────────────────────────────────────────


def make_stdio_url(command: str, args: List[str]) -> str:
    """Generate a stable synthetic URL for a stdio MCP server (used as ``source``)."""
    args_hash = hashlib.sha256(json.dumps(args).encode()).hexdigest()[:8]
    return f"stdio://{command}/{args_hash}"


def derive_server_name(url: str) -> str:
    """Derive a human-readable server name from a URL."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "unknown"
    port = parsed.port
    return f"MCP-{host}:{port}" if port else f"MCP-{host}"


async def _probe_http_status(url: str) -> int:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "method": "ping", "id": 1})
            return resp.status_code
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Probe to {url} failed: {e}")
        return 0


async def build_http_mcp_tool(
    *,
    url: str,
    llm: LLMProvider,
    owner_profile: Optional[str],
    description: Optional[str] = None,
    stored_server_name: Optional[str] = None,
    on_first_connect: Optional[Callable[[MCPAgentAdapter], None]] = None,
    extra: Optional[dict] = None,
) -> MCPServerTool:
    """Connect to an HTTP MCP server (handling 401/no-auth) and return an MCPServerTool."""
    auth_base_url = url.rsplit("/", 1)[0]
    status = await _probe_http_status(url)

    if status == 401:
        # OAuth-required MCP server -- discover OAuth metadata, register a not-yet-connected adapter
        mcp_auth = MCPOAuthClient(auth_base_url, server_name="")
        has_auth = await mcp_auth.discover_auth_metadata()
        if not has_auth:
            raise RuntimeError(
                f"MCP server at {url} requires auth but no OAuth metadata is exposed."
            )
        server_name = stored_server_name or derive_server_name(url)
        mcp_auth.server_name = server_name

        connection = MCPConnection()
        connection._url = url

        adapter = MCPAgentAdapter(
            connection=connection,
            llm=llm,
            mcp_auth=mcp_auth,
            description=description or f"MCP Server at {url} (authentication required)",
            name=server_name,
            on_first_connect=on_first_connect,
        )
        return MCPServerTool(
            url=url, owner_profile=owner_profile, adapter=adapter,
            extra=extra,
        )

    if status == 0:
        raise RuntimeError(f"MCP server at {url} is unreachable")

    # No auth required -- connect immediately and read tools
    connection = MCPConnection()
    await connection.connect_http(url)
    server_name = connection.server_name or stored_server_name or derive_server_name(url)

    mcp_auth = MCPOAuthClient(auth_base_url, server_name)
    has_auth = await mcp_auth.discover_auth_metadata()

    adapter = MCPAgentAdapter(
        connection=connection,
        llm=llm,
        mcp_auth=mcp_auth if has_auth else None,
        description=description,
        name=server_name,
    )
    return MCPServerTool(
        url=url, owner_profile=owner_profile, adapter=adapter,
        transport_type="http", extra=extra,
    )


async def build_stdio_mcp_tool(
    *,
    command: str,
    args: List[str],
    env: Optional[Dict[str, str]],
    llm: LLMProvider,
    owner_profile: Optional[str],
    description: Optional[str] = None,
    extra: Optional[dict] = None,
) -> MCPServerTool:
    """Spawn a stdio MCP server subprocess and return an MCPServerTool."""
    connection = MCPConnection()
    try:
        await connection.connect_stdio(command, args, env)
    except Exception:
        await connection.cleanup()
        raise

    server_name = connection.server_name or f"MCP-{command}"
    adapter = MCPAgentAdapter(
        connection=connection,
        llm=llm,
        mcp_auth=None,
        description=description,
        name=server_name,
    )
    return MCPServerTool(
        url=make_stdio_url(command, args),
        owner_profile=owner_profile,
        adapter=adapter,
        extra=extra,
    )


def build_mcp_stub(
    *,
    url: str,
    name: str,
    owner_profile: Optional[str],
    error: Optional[str] = None,
    description: Optional[str] = None,
    extra: Optional[dict] = None,
) -> MCPServerTool:
    """Build a stub MCPServerTool (disabled or connection-failed)."""
    from a2a.types import AgentCapabilities

    fallback_desc = (
        f"Connection failed: {error}" if error else (description or f"MCP server at {url}")
    )
    card = AgentCard(
        name=name,
        description=fallback_desc,
        url=url,
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[],
    )

    # Build a placeholder adapter that won't actually be invoked
    class _StubAdapter:
        name = card.name
        description = card.description

        def get_skills(self):
            return []

        def create_synthetic_card(self):
            return card

        def update_config(self, **kwargs):
            return None

        async def request(self, *args, **kwargs):
            if False:
                yield None

        @property
        def _llm(self):
            return None

    stub = _StubAdapter()
    tool = MCPServerTool(
        url=url, owner_profile=owner_profile, adapter=stub,  # type: ignore[arg-type]
        connection_error=error,
        extra=extra,
    )
    return tool
