"""Built-in tool adapter.

Makes built-in tools behave like an A2A agent by:
1. Converting BuiltInTool definitions to AgentCard skills
2. Using an LLM to process queries with built-in tools
3. Producing synthetic A2A events for parse_agent_events()

This is the in-process replacement for MCPAgentAdapter (which routes through
MCP stdio transport). The event stream produced is identical.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Dict, List, Optional

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

# OpenAI types are used only in PEP 563-stringified annotations; keep
# them under TYPE_CHECKING so the built-in tool adapter loads without
# the ``llm-openai`` extras group installed.
if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

from app.config.settings import BaseConfig, get_user_working_directory
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import LLMProvider
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.mcp.mcp_auth import MCPOAuthClient
from app.utils.context_storage import get_context
from app.utils.logger import logger

class BuiltInToolAdapter:
    """Wraps built-in tools to behave like an A2A agent.

    The Reasoning Agent interacts with this adapter the same way it would
    with an MCPAgentAdapter or A2A remote agent -- through request() which
    yields A2A events.
    """

    def __init__(
        self,
        tools: List[BuiltInTool],
        llm: Optional[LLMProvider] = None,
        mcp_auth: Optional[MCPOAuthClient] = None,
        description: Optional[str] = None,
        name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        tool_instructions: Optional[str] = None,
        prepare_tools: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        full_reasoning: bool = False,
    ):
        self._tools = tools
        self._tools_by_name: Dict[str, BuiltInTool] = {t.name: t for t in tools}
        self._llm = llm
        self._mcp_auth = mcp_auth
        self._description = description
        self._name = name or ""
        self._context_storage: Dict[str, str] = {}
        self._system_prompt = system_prompt
        self._server_instructions = tool_instructions or ""
        self._prepare_tools = prepare_tools
        self._full_reasoning = full_reasoning

    @property
    def name(self) -> str:
        """The server name (used as agent name)."""
        return self._name

    @property
    def description(self) -> str:
        """Human-readable description.

        Priority: explicit description > server instructions > fallback from tool names.
        """
        if self._description:
            return self._description
        if self._server_instructions:
            return self._server_instructions
        tool_names = [t.name for t in self._tools]
        return f"Built-in tools: {', '.join(tool_names)}"

    def get_skills(self) -> List[AgentSkill]:
        """Convert built-in tools to A2A AgentSkill objects."""
        skills = []
        for tool in self._tools:
            skills.append(AgentSkill(
                id=tool.name,
                name=tool.name,
                description=tool.description or tool.name,
                tags=[],
                examples=[],
            ))
        return skills

    def create_synthetic_card(self) -> AgentCard:
        """Create a synthetic AgentCard for this built-in tool server.

        This card is used by _format_agents_info() and the API
        so that built-in tools appear exactly like A2A agents.
        """
        return AgentCard(
            name=self.name,
            description=self.description,
            url=f"builtin://{self.name}",
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
        """Update adapter configuration in-place.

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

    def build_specs(
        self,
        query: str = "",
        arguments: Optional[Dict[str, Any]] = None,
        context_id: Optional[str] = None,
        profile: str = "default",
    ) -> List[Dict[str, Any]]:
        """Return OpenAI-format function specs for this group's sub-tools.

        Runs the optional ``prepare_tools`` callback so per-request
        customization (dynamic enums sourced from caller-injected ``arguments``
        or conversation-scoped ``ContextStorage`` state, e.g.
        ``change_working_directory``'s loaded-skill enum, ``system_file``'s
        event-run suppression) is reflected in the schemas the model sees.
        """
        available_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": copy.deepcopy(tool.parameters)
                    if tool.parameters else {"type": "object", "properties": {}},
                },
            }
            for tool in self._tools
        ]
        if self._prepare_tools:
            try:
                available_tools = self._prepare_tools(
                    query,
                    available_tools,
                    arguments=arguments,
                    context_id=context_id,
                    profile=profile,
                )
            except Exception as e:
                logger.warning(f"prepare_tools callback failed for '{self.name}': {e}")
        return available_tools

    async def request(
        self,
        query: str,
        context_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        profile: str = "default",
        decided_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Any, None]:
        """Execute one or more decided sub-tool calls and yield synthetic A2A events.

        Native function calling means the reasoning model has already chosen the
        sub-tool(s) and their typed arguments, passed here as ``decided_calls``
        (``[{"name", "arguments"}]``). There is no inner routing LLM: this method
        injects runtime args, runs the tool(s), and produces the same event
        stream that ``parse_agent_events()`` consumes.
        """
        task_id = str(uuid.uuid4())
        context_id = context_id or str(uuid.uuid4())

        # Store context mapping
        self._context_storage[context_id] = task_id

        if not self._llm:
            yield self._make_error_event(
                context_id, task_id,
                f"No LLM configured for built-in tool adapter '{self.name}'. "
                "Check that setup is complete and a model is configured.",
            )
            return

        logger.info(
            f"Built-in adapter '{self.name}' executing "
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

        def _fold_result_usage(result: Any) -> None:
            """Fold a tool result's internal-LLM token usage into the running totals.

            A built-in tool that makes its own LLM call (e.g. documentation_search's
            judge, image_understanding's vision call) reports it on
            ``BuiltInToolResult.token_usage``; tools that make no LLM call leave it
            ``None`` and contribute nothing. The folded totals flow into the
            ``token_usage`` artifact below and on to a ``source_kind="tool"`` record.
            """
            nonlocal total_input_tokens, total_cache_read_input_tokens
            nonlocal total_cache_creation_input_tokens, total_output_tokens
            tu = getattr(result, "token_usage", None)
            if not tu:
                return
            total_input_tokens += tu.get("input_tokens", 0) or 0
            total_cache_read_input_tokens += tu.get("cache_read_input_tokens", 0) or 0
            total_cache_creation_input_tokens += tu.get("cache_creation_input_tokens", 0) or 0
            total_output_tokens += tu.get("output_tokens", 0) or 0

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

                # Inject access token for tools with auth
                if self._mcp_auth:
                    token = self._mcp_auth.get_token(profile)
                    if token:
                        tool_args["_access_token"] = token

                # Inject profile so tools use the correct profile directory
                if profile and "profile" in tool_args:
                    tool_args["profile"] = profile

                # Inject the User Working Directory (default ~/Documents,
                # configurable via the setup wizard) as the active path for
                # all built-in tools. This is intentionally distinct from
                # CREMIND_SYSTEM_DIR/<profile>, which is reserved for
                # Cremind-internal storage (skills, persona, exec_shell stdout).
                # ``change_working_directory`` may store a per-conversation
                # override under ``_working_directory_override`` -- honor it.
                override = (
                    get_context(context_id, "_working_directory_override")
                    if context_id else None
                )
                tool_args["_working_directory"] = override or get_user_working_directory()

                # Inject the active profile name so tools can scope per-profile
                # state (e.g. browser keeps a separate Chrome profile per Cremind profile).
                tool_args["_profile"] = profile

                # Reasoning-agent context_id (== conversation.context_id), so a
                # tool can resolve the calling conversation when needed.
                tool_args["_context_id"] = context_id

                # Hand the adapter's child LLM to the tool so an internal
                # LLM-as-judge step (e.g. ``documentation_search`` picking the
                # most accurate vector-search candidate) reuses the same
                # configured provider/model as the routing pass instead of
                # re-resolving from settings. Tools that don't need an LLM
                # simply ignore this key.
                tool_args["_llm"] = self._llm

                # Inject arguments from metadata (e.g., latitude/longitude)
                if metadata and "arguments" in metadata:
                    meta_arguments = metadata["arguments"]
                    if isinstance(meta_arguments, dict):
                        for arg_key, arg_value in meta_arguments.items():
                            if arg_key not in tool_args:
                                tool_args[arg_key] = arg_value

                # Inject tool config variables so tools can access
                # required_config values (e.g. API keys, URLs) at runtime.
                if metadata and "variables" in metadata:
                    tool_args["_variables"] = metadata["variables"]

                logger.info(f"Built-in adapter executing tool '{tool_name}' with args: {tool_args}")

                tool = self._tools_by_name.get(tool_name)
                if not tool:
                    logger.error(f"Built-in tool '{tool_name}' not found")
                    tool_results[tool_name] = {"error": f"Tool '{tool_name}' not found"}
                    continue

                tool_timeout = BaseConfig.MCP_TOOL_CALL_TIMEOUT
                try:
                    if tool_timeout is not None:
                        result = await asyncio.wait_for(
                            tool.run(tool_args), timeout=tool_timeout
                        )
                    else:
                        result = await tool.run(tool_args)
                    result_data = self._extract_tool_result(result)
                    _fold_result_usage(result)
                    tool_results[tool_name] = result_data
                    logger.info(f"Built-in tool '{tool_name}' result received")
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Built-in tool '{tool_name}' timed out after {tool_timeout}s"
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
                                if tool_timeout is not None:
                                    result = await asyncio.wait_for(
                                        tool.run(tool_args), timeout=tool_timeout
                                    )
                                else:
                                    result = await tool.run(tool_args)
                                result_data = self._extract_tool_result(result)
                                _fold_result_usage(result)
                                tool_results[tool_name] = result_data
                                logger.info(f"Built-in tool '{tool_name}' succeeded after token refresh")
                                continue
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"Built-in tool '{tool_name}' timed out after token refresh ({tool_timeout}s)"
                                )
                                tool_results[tool_name] = {
                                    "error": "Timeout",
                                    "message": f"Tool '{tool_name}' did not respond within {tool_timeout} seconds after auth retry.",
                                }
                                continue
                            except Exception as retry_e:
                                logger.error(f"Built-in tool '{tool_name}' failed after refresh: {retry_e}")
                                tool_results[tool_name] = {"error": str(retry_e)}
                                continue
                    logger.error(f"Built-in tool '{tool_name}' failed: {e}")
                    tool_results[tool_name] = {"error": str(e)}

            # Build artifact parts from tool results.
            artifact_parts: list[Part] = []
            has_file_parts = False

            for _tool_name, result_data in tool_results.items():
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
                        text_content = parsed.get("text", "")
                        if text_content:
                            artifact_parts.append(Part(root=TextPart(text=text_content)))
                        for file_entry in files_list:
                            if isinstance(file_entry, dict) and "uri" in file_entry:
                                artifact_parts.append(Part(root=FilePart(
                                    file=FileWithUri(
                                        uri=file_entry["uri"],
                                        name=file_entry.get("name"),
                                        mime_type=file_entry.get("mime_type"),
                                    ),
                                )))

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
        final_message = None
        if not tool_results and not content_parts:
            final_message = Message(
                messageId=str(uuid.uuid4()),
                role=Role.agent,
                parts=[Part(root=TextPart(text="No results from built-in tools."))],
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
        """Extract usable data from a BuiltInToolResult.

        Handles the same content shapes as MCPAgentAdapter._extract_tool_result():
        - structured_content (dict)
        - content (list of items with .text/.data attributes or dicts)
        """
        if not result:
            return None

        # BuiltInToolResult.structured_content takes priority (most tools use this)
        if hasattr(result, 'structured_content') and result.structured_content is not None:
            return result.structured_content

        # BuiltInToolResult.content (list of content items)
        if hasattr(result, 'content') and result.content:
            parts_data = []
            for item in result.content:
                # Handle dict content items (e.g., {"type": "text", "text": "..."})
                if isinstance(item, dict):
                    if "text" in item:
                        parts_data.append(item["text"])
                    elif "data" in item:
                        parts_data.append(item["data"])
                    else:
                        parts_data.append(item)
                # Handle object content items (for compatibility)
                elif hasattr(item, 'text'):
                    parts_data.append(item.text)
                elif hasattr(item, 'data'):
                    parts_data.append(item.data)
                elif hasattr(item, 'structured_content'):
                    parts_data.append(item.structured_content)
            if len(parts_data) == 1:
                return parts_data[0]
            return parts_data

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
