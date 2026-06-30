"""MCP-to-Agent bridge adapter.

Makes an MCP server behave like an A2A agent by:
1. Converting MCP tools to AgentCard skills
2. Using an LLM to process queries with MCP tools
3. Producing synthetic A2A events for parse_agent_events()
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, List, Optional

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    FilePart,
    FileWithUri,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

# OpenAI types are only used as annotations (PEP 563-stringified) so
# loading them under TYPE_CHECKING keeps the MCP adapter importable on
# a thin-core install.
if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

from app.config.settings import BaseConfig
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import LLMProvider
from app.tools.mcp.mcp_auth import MCPOAuthClient
from app.tools.mcp.mcp_connection import MCPConnection
from app.utils.logger import logger

MCP_AGENT_SYSTEM_PROMPT = (
    "You are an AI Agent returning results from tool calls. "
    "Use the available tools to answer the user's query. "
    "Always call the appropriate tool(s) to get the data needed to answer."
)


class MCPAgentAdapter:
    """Wraps an MCP server connection to behave like an A2A agent.

    The Reasoning Agent interacts with this adapter the same way it would
    with an A2A remote agent - through request() which yields A2A events.
    """

    def __init__(
        self,
        connection: MCPConnection,
        llm: LLMProvider,
        mcp_auth: Optional[MCPOAuthClient] = None,
        description: Optional[str] = None,
        name: Optional[str] = None,
        on_first_connect=None,
        system_prompt: Optional[str] = None,
        full_reasoning: bool = False,
    ):
        self._connection = connection
        self._llm = llm
        self._mcp_auth = mcp_auth
        self._description = description
        self._name = name
        self._context_storage: Dict[str, str] = {}
        self._auth_lock = asyncio.Lock()
        self._on_first_connect = on_first_connect
        self._system_prompt = system_prompt
        self._full_reasoning = full_reasoning

    @property
    def name(self) -> str:
        """The MCP server name (used as agent name)."""
        return self._connection.server_name or self._name or ""

    @property
    def description(self) -> str:
        """Human-readable description of the MCP server.

        Priority: explicit description > server instructions > fallback from tool names.
        """
        if self._description:
            return self._description
        if self._connection.tool_instructions:
            return self._connection.tool_instructions
        tool_names = [t.name for t in self._connection.get_tools()]
        return f"MCP Server providing tools: {', '.join(tool_names)}"

    def get_skills(self) -> List[AgentSkill]:
        """Convert MCP tools to A2A AgentSkill objects.

        Each MCP tool becomes a skill with the tool's name and description.
        """
        skills = []
        for tool in self._connection.get_tools():
            skills.append(AgentSkill(
                id=tool.name,
                name=tool.name,
                description=tool.description or tool.name,
                tags=[],
                examples=[],
            ))
        return skills

    def create_synthetic_card(self) -> AgentCard:
        """Create a synthetic AgentCard for this MCP server.

        This card is used by _format_agents_info() and the API
        so that MCP servers appear exactly like A2A agents.
        """
        return AgentCard(
            name=self.name,
            description=self.description,
            url=self._connection.url or f"stdio://{self.name}",
            version="1.0.0",
            defaultInputModes=["text"],
            defaultOutputModes=["text"],
            capabilities=AgentCapabilities(streaming=True),
            skills=self.get_skills(),
        )

    def get_context_storage(self) -> Dict[str, str]:
        """Get the context_id -> task_id mapping."""
        return self._context_storage

    def update_config(
        self,
        llm: Optional[LLMProvider] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        full_reasoning: Optional[bool] = None,
    ) -> None:
        """Update adapter configuration in-place without disrupting MCP connection.

        Only non-None values are applied. Pass empty string to clear a field.
        """
        if llm is not None:
            self._llm = llm
        if system_prompt is not None:
            self._system_prompt = system_prompt if system_prompt else None
        if description is not None:
            self._description = description if description else None
        if full_reasoning is not None:
            self._full_reasoning = full_reasoning

    async def _ensure_auth(self, profile: str):
        """Ensure HTTP connection has current auth token.

        For HTTP MCP servers: connects/reconnects with Bearer token if needed.
        Handles two cases:
        1. Initial connection after 401 at add time (session is None)
        2. Token changed/refreshed (reconnect with new token)

        No-op for stdio servers (they use _access_token injection in tool args).
        """
        if not self._mcp_auth:
            return
        if self._connection.transport_type not in ("http", ""):
            return

        token = self._mcp_auth.get_token(profile)
        if not token:
            return

        # Case 1: Never connected (401 at add time)
        if self._connection.session is None and self._connection.url:
            async with self._auth_lock:
                if self._connection.session is not None:
                    return
                logger.info(f"Connecting MCP server '{self.name}' with auth token (first connection)")
                headers = {"Authorization": f"Bearer {token}"}
                await self._connection.connect_http(self._connection.url, headers=headers)

                # Clear placeholder description so real server info takes over
                self._description = None

                # Notify routing_agent to update agents_info with real server info
                if self._on_first_connect:
                    self._on_first_connect(self)
                    self._on_first_connect = None
            return

        # Case 2: Already connected, check if token changed
        if self._connection.current_auth_token == token:
            return

        async with self._auth_lock:
            if self._connection.current_auth_token == token:
                return
            logger.info(f"Reconnecting MCP server '{self.name}' with updated auth token")
            headers = {"Authorization": f"Bearer {token}"}
            await self._connection.reconnect_http(headers=headers)

    def build_specs(
        self,
        query: str = "",
        arguments: Optional[Dict] = None,
        context_id: Optional[str] = None,
        profile: str = "default",
    ) -> List[Dict[str, Any]]:
        """Return OpenAI-format function specs for this server's MCP tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
                },
            }
            for tool in self._connection.get_tools()
        ]

    async def request(
        self,
        query: str,
        context_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        profile: str = "default",
        decided_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Any, None]:
        """Execute decided MCP tool call(s) and yield synthetic A2A events.

        Native function calling means the reasoning model already picked the
        MCP tool(s) and arguments (``decided_calls``); there is no inner routing
        LLM. The event stream stays compatible with ``parse_agent_events()``.
        """
        task_id = str(uuid.uuid4())
        context_id = context_id or str(uuid.uuid4())

        # Store context mapping
        self._context_storage[context_id] = task_id

        # Ensure HTTP connection has auth token (reconnects if needed)
        await self._ensure_auth(profile)

        logger.info(
            f"MCP adapter '{self.name}' executing "
            f"{[c.get('name') for c in (decided_calls or [])]}"
        )

        # Yield working status
        yield TaskStatusUpdateEvent(
            contextId=context_id,
            taskId=task_id,
            final=False,
            status=TaskStatus(state=TaskState.working),
        )

        total_input_tokens = 0
        total_cache_read_input_tokens = 0
        total_cache_creation_input_tokens = 0
        total_output_tokens = 0
        content_parts: List[str] = []
        function_calls: List[Dict] = list(decided_calls or [])

        # Execute tool calls if any
        tool_results: Dict[str, Any] = {}
        if function_calls:
            for func_call in function_calls:
                tool_name = func_call.get("name", "")
                tool_args = func_call.get("arguments", {})
                if isinstance(tool_args, str):
                    try:
                        tool_args = json.loads(tool_args)
                    except json.JSONDecodeError:
                        tool_args = {}

                # Inject access token for stdio servers with auth
                if self._mcp_auth and self._connection.transport_type == "stdio":
                    token = self._mcp_auth.get_token(profile)
                    if token:
                        tool_args["_access_token"] = token

                # Inject profile so tools use the correct profile directory
                if profile and "profile" in tool_args:
                    tool_args["profile"] = profile

                logger.info(f"MCP adapter executing tool '{tool_name}' with args: {tool_args}")

                tool_timeout = BaseConfig.MCP_TOOL_CALL_TIMEOUT
                try:
                    result = await self._connection.call_tool(tool_name, tool_args, timeout=tool_timeout)
                    # Extract result content
                    result_data = self._extract_tool_result(result)
                    tool_results[tool_name] = result_data
                    logger.info(f"MCP tool '{tool_name}' result received")
                except asyncio.TimeoutError:
                    logger.warning(
                        f"MCP tool '{tool_name}' timed out after {tool_timeout}s"
                    )
                    tool_results[tool_name] = {
                        "error": "Timeout",
                        "message": f"Tool '{tool_name}' did not respond within {tool_timeout} seconds.",
                    }
                except Exception as e:
                    error_str = str(e)
                    # If auth error, try refreshing token and retry once
                    if self._mcp_auth and ("401" in error_str or "unauthorized" in error_str.lower()
                                           or "Authentication required" in error_str):
                        logger.info(f"Auth error for tool '{tool_name}', attempting token refresh...")
                        refreshed = await self._mcp_auth.refresh_access_token(profile)
                        if refreshed:
                            new_token = self._mcp_auth.get_token(profile)
                            if new_token:
                                tool_args["_access_token"] = new_token
                            try:
                                result = await self._connection.call_tool(
                                    tool_name, tool_args, timeout=tool_timeout
                                )
                                result_data = self._extract_tool_result(result)
                                tool_results[tool_name] = result_data
                                logger.info(f"MCP tool '{tool_name}' succeeded after token refresh")
                                continue
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"MCP tool '{tool_name}' timed out after token refresh ({tool_timeout}s)"
                                )
                                tool_results[tool_name] = {
                                    "error": "Timeout",
                                    "message": f"Tool '{tool_name}' did not respond within {tool_timeout} seconds after auth retry.",
                                }
                                continue
                            except Exception as retry_e:
                                logger.error(f"MCP tool '{tool_name}' failed after refresh: {retry_e}")
                                tool_results[tool_name] = {"error": str(retry_e)}
                                continue
                    logger.error(f"MCP tool '{tool_name}' failed: {e}")
                    tool_results[tool_name] = {"error": str(e)}

            # Build artifact parts from tool results.
            # Tools that return files use the ToolResultWithFiles format
            # (see app.types.ToolResultWithFiles): a dict with "text" and
            # "_files" keys.  The adapter converts each entry in "_files"
            # into an A2A FilePart(file=FileWithUri(…)).
            artifact_parts: list[Part] = []
            has_file_parts = False

            for _tool_name, result_data in tool_results.items():
                # MCP tools return structured_content as a JSON string;
                # parse it back into a dict so we can inspect the shape.
                parsed = result_data
                if isinstance(parsed, str):
                    try:
                        parsed = json.loads(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass

                if isinstance(parsed, dict) and "_files" in parsed:
                    files_list = parsed["_files"]
                    if isinstance(files_list, list) and files_list:
                        has_file_parts = True
                        # Observation text for the LLM
                        text_content = parsed.get("text", "")
                        if text_content:
                            artifact_parts.append(Part(root=TextPart(text=text_content)))
                        # Convert each ToolResultFile → FilePart
                        for file_entry in files_list:
                            if isinstance(file_entry, dict) and "uri" in file_entry:
                                artifact_parts.append(Part(root=FilePart(
                                    file=FileWithUri(
                                        uri=file_entry["uri"],
                                        name=file_entry.get("name"),
                                        mime_type=file_entry.get("mime_type"),
                                    ),
                                )))

            # If no _files found, fall back to a single DataPart.
            # Parse any JSON-string values into dicts so downstream
            # formatting can produce readable text instead of raw JSON.
            if not has_file_parts and tool_results:
                parsed_results = {}
                for tn, rd in tool_results.items():
                    if isinstance(rd, str):
                        try:
                            parsed_results[tn] = json.loads(rd)
                        except (json.JSONDecodeError, TypeError):
                            parsed_results[tn] = rd
                    else:
                        parsed_results[tn] = rd
                artifact_parts = [Part(root=DataPart(data=parsed_results))]

            if artifact_parts:
                yield TaskArtifactUpdateEvent(
                    contextId=context_id,
                    taskId=task_id,
                    artifact=Artifact(
                        artifactId=str(uuid.uuid4()),
                        name="tool_results",
                        parts=artifact_parts,
                    ),
                )

        # If we have text content from LLM, yield it as artifact
        if content_parts:
            yield TaskArtifactUpdateEvent(
                contextId=context_id,
                taskId=task_id,
                artifact=Artifact(
                    artifactId=str(uuid.uuid4()),
                    name="response",
                    parts=[Part(root=TextPart(text=" ".join(content_parts)))],
                ),
            )

        # Yield token_usage artifact
        yield TaskArtifactUpdateEvent(
            contextId=context_id,
            taskId=task_id,
            artifact=Artifact(
                artifactId=str(uuid.uuid4()),
                name="token_usage",
                parts=[Part(root=DataPart(data={
                    "token_usage": {
                        "input_tokens": total_input_tokens,
                        "cache_read_input_tokens": total_cache_read_input_tokens,
                        "cache_creation_input_tokens": total_cache_creation_input_tokens,
                        "output_tokens": total_output_tokens,
                    }
                }))],
            ),
        )

        # Yield completed status.
        # Data is already in artifact events above, so only add a message
        # when there are no artifacts at all.
        final_message = None
        if not tool_results and not content_parts:
            final_message = Message(
                messageId=str(uuid.uuid4()),
                role=Role.agent,
                parts=[Part(root=TextPart(text="No results from MCP server."))],
            )

        yield TaskStatusUpdateEvent(
            contextId=context_id,
            taskId=task_id,
            final=True,
            status=TaskStatus(
                state=TaskState.completed,
                message=final_message,
            ),
        )

        # Clean up context storage
        self._context_storage.pop(context_id, None)

    def _extract_tool_result(self, result: Any) -> Any:
        """Extract usable data from an MCP tool result."""
        if not result:
            return None

        # MCP CallToolResult has .content which is a list of content items
        if hasattr(result, 'content') and result.content:
            parts_data = []
            for item in result.content:
                if hasattr(item, 'text'):
                    parts_data.append(item.text)
                elif hasattr(item, 'data'):
                    parts_data.append(item.data)
                elif hasattr(item, 'structured_content'):
                    parts_data.append(item.structured_content)
            if len(parts_data) == 1:
                return parts_data[0]
            return parts_data

        # Fallback: if result has structured_content
        if hasattr(result, 'structured_content'):
            return result.structured_content

        return str(result)

    def _make_error_event(self, context_id: str, task_id: str, error_msg: str) -> TaskStatusUpdateEvent:
        """Create a failed status event."""
        return TaskStatusUpdateEvent(
            contextId=context_id,
            taskId=task_id,
            final=True,
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    messageId=str(uuid.uuid4()),
                    role=Role.agent,
                    parts=[Part(root=TextPart(text=f"Error: {error_msg}"))],
                ),
            ),
        )
