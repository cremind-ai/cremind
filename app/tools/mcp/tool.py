"""MCP server wrapped as a unified :class:`Tool`.

An MCP server can talk MCP-protocol tools, but the reasoning agent never sees
that protocol -- it sees a single registry tool whose ``execute()`` returns
observation parts. The child LLM here selects which MCP-protocol tool(s) to
call; this is the "low" model group (or per-server override).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.parse
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from a2a.types import AgentCard

from app.config.settings import BaseConfig
from app.lib.llm.base import LLMProvider
from app.tools.base import (
    Tool,
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
        transport_type: str = "http",
        connection_error: Optional[str] = None,
        extra: Optional[dict] = None,
        llm_factory: Optional[Callable[[str], LLMProvider]] = None,
    ):
        super().__init__()
        self._url = url
        self._owner_profile = owner_profile
        self._adapter = adapter
        self._transport_type = transport_type
        self._connection_error = connection_error
        self._extra = extra or {}
        self._llm_factory = llm_factory

    # ── identity ────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return self._url

    @property
    def owner_profile(self) -> Optional[str]:
        return self._owner_profile

    @property
    def transport_type(self) -> str:
        return self._transport_type

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
    def extra(self) -> dict:
        return self._extra

    @property
    def skills(self) -> List[ToolSkill]:
        return [
            ToolSkill(id=s.id, name=s.name, description=s.description)
            for s in self._adapter.get_skills()
        ]

    def get_card(self) -> AgentCard:
        return self._adapter.create_synthetic_card()

    # ── runtime LLM refresh ────────────────────────────────────────────

    def refresh_llm(self, profile: str) -> None:
        """Re-create the child LLM from current config (called before model-label read)."""
        if self._llm_factory:
            try:
                llm = self._llm_factory(profile)
                self._adapter.update_config(llm=llm)
            except Exception:  # noqa: BLE001
                pass  # keep current LLM; adapter handles the "no LLM" case

    # ── runtime updates ─────────────────────────────────────────────────

    def update_runtime_config(
        self,
        *,
        llm: Optional[LLMProvider] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        full_reasoning: Optional[bool] = None,
    ) -> None:
        self._adapter.update_config(
            llm=llm, system_prompt=system_prompt,
            description=description, full_reasoning=full_reasoning,
        )

    def mark_connection_error(self, error: str) -> None:
        self._connection_error = error

    def clear_connection_error(self) -> None:
        self._connection_error = None

    # ── execution ───────────────────────────────────────────────────────

    async def execute(
        self,
        *,
        query: str,
        context_id: str,
        profile: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
        llm_params: Dict[str, Any],
    ) -> AsyncGenerator[ToolEvent, None]:
        if self._connection_error:
            yield ToolErrorEvent(
                message=(
                    f"MCP server '{self.name}' is unavailable: {self._connection_error}. "
                    "Please reconnect or disable this tool."
                )
            )
            return

        # Refresh the child LLM so config changes take effect without restart.
        # (Also called by the reasoning agent before reading Model_Label.)
        self.refresh_llm(profile)

        # Apply per-call llm_params (e.g., full_reasoning override)
        if "full_reasoning" in llm_params:
            self._adapter.update_config(full_reasoning=bool(llm_params["full_reasoning"]))

        yield ToolThinkingEvent(
            text=f"[{self.name}] {query}",
            model_label=getattr(self._adapter._llm, "model_label", None) if self._adapter._llm else None,
        )

        events: list = []
        metadata = {"arguments": arguments} if arguments else None

        try:
            async for ev in self._adapter.request(
                query=query, context_id=context_id, metadata=metadata, profile=profile,
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
    system_prompt: Optional[str] = None,
    description: Optional[str] = None,
    stored_server_name: Optional[str] = None,
    full_reasoning: bool = False,
    on_first_connect: Optional[Callable[[MCPAgentAdapter], None]] = None,
    extra: Optional[dict] = None,
    llm_factory: Optional[Callable[[str], LLMProvider]] = None,
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
            system_prompt=system_prompt,
            full_reasoning=full_reasoning,
        )
        return MCPServerTool(
            url=url, owner_profile=owner_profile, adapter=adapter,
            transport_type="http", extra=extra, llm_factory=llm_factory,
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
        system_prompt=system_prompt,
        full_reasoning=full_reasoning,
    )
    return MCPServerTool(
        url=url, owner_profile=owner_profile, adapter=adapter,
        transport_type="http", extra=extra, llm_factory=llm_factory,
    )


async def build_stdio_mcp_tool(
    *,
    command: str,
    args: List[str],
    env: Optional[Dict[str, str]],
    llm: LLMProvider,
    owner_profile: Optional[str],
    system_prompt: Optional[str] = None,
    description: Optional[str] = None,
    full_reasoning: bool = False,
    extra: Optional[dict] = None,
    llm_factory: Optional[Callable[[str], LLMProvider]] = None,
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
        system_prompt=system_prompt,
        full_reasoning=full_reasoning,
    )
    return MCPServerTool(
        url=make_stdio_url(command, args),
        owner_profile=owner_profile,
        adapter=adapter,
        transport_type="stdio",
        extra=extra,
        llm_factory=llm_factory,
    )


def build_mcp_stub(
    *,
    url: str,
    name: str,
    owner_profile: Optional[str],
    error: Optional[str] = None,
    description: Optional[str] = None,
    transport_type: str = "http",
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
        transport_type=transport_type, connection_error=error,
        extra=extra,
    )
    return tool
