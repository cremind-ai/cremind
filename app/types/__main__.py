from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, TypedDict, Required, NotRequired, TYPE_CHECKING

from a2a.types import (
    AgentCard
)

if TYPE_CHECKING:
    import pandas as pd
    from app.tools.a2a import RemoteAgentConnections

from app.constants import ChatCompletionTypeEnum


class EmbeddingTable:
    """Type-safe wrapper for embedding DataFrames.

    This class ensures that DataFrames containing embeddings are not confused
    with generic DataFrames and provides a clear semantic type for embedding operations.

    The underlying DataFrame must have columns: 'id', 'text', and 'embeddings'.
    """

    def __init__(self, df: pd.DataFrame):
        """Initialize EmbeddingTable with a DataFrame.

        Args:
            df: DataFrame with columns 'id', 'text', and 'embeddings'

        Raises:
            ValueError: If required columns are missing
        """
        required_columns = {'id', 'text', 'embeddings'}
        if not required_columns.issubset(df.columns):
            raise ValueError(
                f"DataFrame must contain columns: {required_columns}. "
                f"Got: {set(df.columns)}"
            )
        self._df = df

    @property
    def dataframe(self) -> pd.DataFrame:
        """Get the underlying DataFrame."""
        return self._df

    def __len__(self) -> int:
        """Return the number of rows in the table."""
        return len(self._df)

    def is_empty(self) -> bool:
        """Check if the embedding table is empty."""
        return self._df.empty


class ToolEmbeddingRecord(TypedDict):
    """Metadata-bearing input row for tool-card embeddings.

    Flows from ``_collect_tool_data`` through ``build_table_embeddings`` into
    the Qdrant payload so retrieval can filter by ``tool_type`` / ``tool_id`` /
    ``enabled`` without joining back to the registry.
    """
    text: str
    tool_id: str
    name: str
    tool_type: str
    enabled: bool


class MCPServerConfig(TypedDict):
    """Configuration for an MCP server, persisted in storage."""
    url: str
    llm_provider: NotRequired[Optional[str]]    # "groq"|"openai"|"ollama"|"vertexai"|"vllm"
    llm_model: NotRequired[Optional[str]]        # e.g. "openai/gpt-oss-20b"
    system_prompt: NotRequired[Optional[str]]     # custom system prompt for this agent
    description: NotRequired[Optional[str]]       # agent description override


class AgentInfo(TypedDict):
    remote_agent_connections: "RemoteAgentConnections"
    context_storage: Dict[str, str]
    card: AgentCard
    url: str
    arguments_schema: NotRequired[Optional[Dict[str, Any]]]
    agent_type: NotRequired[str]  # "a2a" (default) or "mcp"
    mcp_adapter: NotRequired[Any]  # MCPAgentAdapter instance (only for agent_type="mcp")
    is_default: NotRequired[bool]  # True for stdio MCP servers defined in source code
    enabled: NotRequired[bool]  # False to exclude from system prompt tools (default: True)
    profile: NotRequired[str]  # Owning profile name, "__shared__" for stdio MCP servers
    connection_error: NotRequired[Optional[str]]  # Error message when connection failed at startup
    is_stub: NotRequired[bool]  # True when registered via register_stub (not connected)
    config_name: NotRequired[Optional[str]]  # Links to tool config (e.g., tool_name for built-in, skill name for skills)
    skill_info: NotRequired[Any]  # SkillInfo instance (only for agent_type='skill')


# ---------------------------------------------------------------------------
# Tool result file types — canonical format for MCP tools returning files.
#
# Any built-in MCP tool that needs to surface a file to the frontend should
# return a ``ToolResultWithFiles`` dict as its ``structured_content``.
# The MCPAgentAdapter recognises this shape and converts each entry in
# ``_files`` into an A2A ``FilePart(file=FileWithUri(…))``.
#
# Single-file shorthand: a tool may set ``_files`` to a list with one item.
# ---------------------------------------------------------------------------

class ToolResultFile(TypedDict):
    """One file returned by an MCP tool.

    Fields mirror ``a2a.types.FileWithUri`` so the adapter can map 1-to-1:
      - ``uri``       (required) — absolute filesystem path to the file,
                       e.g. ``/home/user/.cremind/report.pdf``.
      - ``name``      (optional) — human-readable filename.
      - ``mime_type``  (optional) — MIME type, e.g. ``image/png``.

    Extra metadata fields are allowed and will be ignored by the adapter.
    """
    uri: str
    name: NotRequired[Optional[str]]
    mime_type: NotRequired[Optional[str]]


class ToolResultWithFiles(TypedDict):
    """Canonical structured_content format for MCP tools that return files.

    Keys:
      - ``text``   — observation text for the LLM.  Can be the full readable
                     content or a short placeholder such as
                     ``[Binary file: photo.png (image/png)]``.
      - ``_files`` — one or more ``ToolResultFile`` dicts to be converted into
                     ``FilePart`` objects by the adapter.

    Example::

        ToolResult(structured_content=ToolResultWithFiles(
            text="Here is the requested image.",
            _files=[
                ToolResultFile(uri="/home/user/.cremind/photo.png",
                               name="photo.png",
                               mime_type="image/png"),
            ],
        ))
    """
    text: str
    _files: list[ToolResultFile]


class RequiredConfigField(TypedDict, total=False):
    """Schema for one environment variable required by a built-in tool."""
    description: Required[str]
    type: Required[str]
    secret: bool
    enum: list[str]
    default: Any


class OAuthConfig(TypedDict, total=False):
    """OAuth provider endpoints and scopes."""
    authorization_endpoint: Required[str]
    token_endpoint: Required[str]
    scopes: list[str]
    extra_authorize_params: dict[str, str]


class LLMParameters(TypedDict, total=False):
    """Default LLM-related parameters a built-in tool ships with.

    User-level overrides are persisted per-profile in the ``llm`` and ``meta``
    scopes of ``tool_configs``; values here are code defaults surfaced to the
    UI as placeholders and used as fallbacks at tool registration / execution.
    """
    tool_instructions: str
    description: str
    system_prompt: str
    llm_provider: str
    llm_model: str
    reasoning_effort: str
    full_reasoning: bool


class ToolConfig(TypedDict, total=False):
    """Static configuration exported as ``TOOL_CONFIG`` by each built-in tool module."""
    name: Required[str]
    display_name: Required[str]
    default_model_group: Required[str]
    visible: bool
    # When True, the tool is still registered with the agent (and remains
    # available at runtime) but is suppressed from the Settings UI and the
    # Setup Wizard catalog. Use for "system" built-ins whose lifecycle is
    # controlled by the runtime, not the user (e.g. ``register_skill_event``):
    # disabling them would break internal flows.
    hidden: bool
    required_config: dict[str, RequiredConfigField]
    oauth: OAuthConfig
    arguments: dict[str, Any]
    llm_parameters: LLMParameters
    # Names of LLM-parameter keys whose user-facing override is forbidden.
    # The settings UI disables the field and the API rejects writes that
    # would change it. Used by tools whose contract depends on a specific
    # LLM-parameter value (e.g. documentation_search keeps
    # ``full_reasoning=False`` because relevance ranking already runs
    # inside the tool).
    locked_llm_fields: list[str]
    # When True, ``BuiltInToolAdapter`` skips its routing LLM call and
    # invokes the group's single sub-tool directly with
    # ``{"query": <Action_Input>}``. Use this for tool groups whose
    # contract is "take the user's input verbatim and do the work" --
    # the routing LLM otherwise just paraphrases the input into a
    # one-tool-call no-op (token waste with no decision to make).
    direct_dispatch: bool
    # Optional Python deps group needed for this tool to run. Maps to a
    # key in ``app.features.manifest.FEATURES``. Used by the Setup Wizard
    # auto-installer and the post-setup enable pre-flight to pip-install
    # the right extras (e.g. ``"browser"`` -> ``cremind[browser]``).
    requires_feature: str
    # When True, the tool stays VISIBLE in the Settings UI but its
    # enable/disable toggle is locked ON: the user cannot turn it off.
    # Unlike ``hidden`` (which removes the tool from the UI entirely),
    # ``locked`` keeps it listed and configurable while guaranteeing it is
    # always exposed to the reasoning agent. The API rejects disable writes;
    # the UI renders the toggle disabled with a lock icon (surfaced to the
    # frontend as the row field ``toggle_locked``).
    locked: bool


class ChatCompletionStreamResponseType(TypedDict):
    type: ChatCompletionTypeEnum
    data: Required[Optional[Any]]
    last_token: NotRequired[bool]
    input_tokens: NotRequired[Optional[int]]
    output_tokens: NotRequired[Optional[int]]
    finish_reason: NotRequired[Optional[str]]


class FunctionCallingResponseType(TypedDict):
    name: str
    index: int
    id: str
    arguments: str


class VectorEmbeddingType(Enum):
    OPENAI = "OPENAI"
    GRPC = "GRPC"


class ReasoningStreamResponseType(TypedDict):
    type: ChatCompletionTypeEnum
    data: Required[Any]
    input_tokens: NotRequired[Optional[int]]
    output_tokens: NotRequired[Optional[int]]
