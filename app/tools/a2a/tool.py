"""A2A remote agent wrapped as a unified :class:`Tool`.

The remote agent has its own LLM -- this side just streams an A2A message and
collapses the resulting event stream into a :class:`ToolResultEvent` for the
reasoning agent.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

import httpx
from a2a.client import A2ACardResolver
from a2a.types import (
    AgentCard,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendStreamingMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

from app.tools.a2a.connection import RemoteAgentConnections
from app.tools.base import (
    Tool,
    ToolErrorEvent,
    ToolEvent,
    ToolResultEvent,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.utils.event_parser import parse_agent_events
from app.utils.logger import logger


class A2ATool(Tool):
    """Wraps an external A2A agent as a registry :class:`Tool`."""

    tool_type = ToolType.A2A

    def __init__(
        self,
        *,
        url: str,
        owner_profile: Optional[str],
        card: AgentCard,
        connection: Optional[RemoteAgentConnections] = None,
        arguments_schema: Optional[dict] = None,
        connection_error: Optional[str] = None,
    ):
        super().__init__()
        self._url = url
        self._owner_profile = owner_profile
        self._card = card
        self._arguments_schema = arguments_schema
        self._connection = connection
        self._connection_error = connection_error
        # context_id -> task_id, used to correlate streaming turns
        self._context_storage: Dict[str, str] = {}

    # ── identity ────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return self._url

    @property
    def owner_profile(self) -> Optional[str]:
        return self._owner_profile

    @property
    def name(self) -> str:
        return self._card.name

    @property
    def description(self) -> str:
        return self._card.description or "No description available"

    @property
    def arguments_schema(self) -> Optional[dict]:
        return self._arguments_schema

    @property
    def connection(self) -> Optional[RemoteAgentConnections]:
        return self._connection

    @property
    def connection_error(self) -> Optional[str]:
        return self._connection_error

    @property
    def is_stub(self) -> bool:
        return self._connection is None

    def get_card(self) -> AgentCard:
        return self._card

    # ── runtime updates ─────────────────────────────────────────────────

    def attach_connection(self, connection: RemoteAgentConnections) -> None:
        self._connection = connection
        self._connection_error = None

    def mark_connection_error(self, error: str) -> None:
        self._connection_error = error

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
                    f"Agent '{self.name}' is unavailable: {self._connection_error}. "
                    "Please reconnect or disable this tool."
                )
            )
            return

        if self._connection is None:
            yield ToolErrorEvent(
                message=f"Agent '{self.name}' is not connected.",
            )
            return

        # Auth check (OAuth)
        try:
            oauth_client = self._connection.get_oauth_client_for_profile(profile)
            auth_status = oauth_client.get_auth_status(profile)
        except Exception:  # noqa: BLE001
            auth_status = "not_supported"

        if auth_status in ("not_authenticated", "expired"):
            verb = "authenticate with" if auth_status == "not_authenticated" else "re-authenticate with"
            yield ToolErrorEvent(
                message=(
                    f"To access {self.name}, you need to {verb} this agent first. "
                    f"Please open the app and complete authentication for this agent."
                ),
                auth_required=True,
            )
            return

        # Yield a thinking event for UI continuity (no model label -- remote agent's own LLM)
        yield ToolThinkingEvent(text=f"[{self.name}] {query}", model_label=None)

        events: list = []
        task_id = self._context_storage.get(context_id)

        request_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        payload = {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": query}],
                "messageId": message_id,
                "contextId": context_id,
                "taskId": task_id,
            },
            "metadata": {"arguments": arguments} if arguments else {},
        }
        message_request = SendStreamingMessageRequest(
            id=request_id, params=MessageSendParams.model_validate(payload),
        )

        try:
            async for chunk in self._connection.send_message_streaming(
                message_request, profile=profile,
            ):
                if hasattr(chunk.root, "error") and chunk.root.error:
                    logger.error(f"JSONRPC error from agent {self.name}: {chunk.root.error}")
                    continue
                event = chunk.root.result if hasattr(chunk.root, "result") else None
                if event is None:
                    continue

                if isinstance(event, Task):
                    ev_ctx = getattr(event, "context_id", None)
                    ev_task = getattr(event, "id", None)
                    if ev_ctx and ev_task:
                        self._context_storage[ev_ctx] = ev_task
                    events.append(event)
                    yield ToolStatusEvent(raw=event)
                    continue

                if isinstance(event, TaskStatusUpdateEvent):
                    state = getattr(event.status, "state", None)
                    if state == TaskState.completed:
                        ev_ctx = getattr(event, "context_id", None)
                        if ev_ctx and ev_ctx in self._context_storage:
                            del self._context_storage[ev_ctx]
                    events.append(event)
                    yield ToolStatusEvent(raw=event)
                    if state in (
                        TaskState.completed, TaskState.failed,
                        TaskState.canceled, TaskState.unknown,
                    ):
                        break
                    continue

                if isinstance(event, (TaskArtifactUpdateEvent, Message)):
                    events.append(event)
                    yield ToolStatusEvent(raw=event)

        except Exception as e:  # noqa: BLE001
            logger.exception(f"A2A tool '{self.name}' failed during request")
            yield ToolErrorEvent(message=str(e))
            return

        observation_text, token_usage, observation_parts = parse_agent_events(events)
        yield ToolResultEvent(
            observation_text=observation_text,
            observation_parts=observation_parts,
            token_usage=token_usage or {},
        )


# ── factory: probe + register ──────────────────────────────────────────────


async def fetch_agent_card(url: str) -> tuple[AgentCard, Optional[dict]]:
    """Resolve the agent card and raw card JSON (for ``arguments`` field)."""
    async with httpx.AsyncClient(timeout=30) as client:
        resolver = A2ACardResolver(client, url)
        card = await resolver.get_agent_card()
        # Fetch raw JSON to capture arguments schema (Pydantic strips unknowns)
        arguments_schema = None
        try:
            resp = await client.get(f"{url.rstrip('/')}/.well-known/agent.json")
            if resp.status_code == 200:
                arguments_schema = resp.json().get("arguments")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to fetch raw agent card from {url}: {e}")
        return card, arguments_schema


async def build_a2a_tool(*, url: str, owner_profile: Optional[str]) -> A2ATool:
    """Connect to an A2A endpoint and return an :class:`A2ATool` ready to use.

    Raises if the endpoint is unreachable -- callers that want stubs should
    catch and fall back to :func:`build_a2a_stub`.
    """
    card, arguments_schema = await fetch_agent_card(url)
    connection = RemoteAgentConnections(agent_card=card, agent_url=url)
    await connection.authenticate()
    return A2ATool(
        url=url,
        owner_profile=owner_profile,
        card=card,
        connection=connection,
        arguments_schema=arguments_schema,
    )


def build_a2a_stub(*, url: str, name: str, owner_profile: Optional[str], error: str) -> A2ATool:
    """Build a stub A2ATool representing an agent we couldn't connect to."""
    from a2a.types import AgentCapabilities  # local import to avoid cycle

    card = AgentCard(
        name=name,
        description=f"Connection failed: {error}",
        url=url,
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[],
    )
    return A2ATool(
        url=url, owner_profile=owner_profile, card=card,
        connection=None, arguments_schema=None,
        connection_error=error,
    )
