from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, TypedDict, Required, NotRequired, TYPE_CHECKING

from a2a.types import (
    AgentCard
)

if TYPE_CHECKING:
    import pandas as pd

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
    remote_agent_connections: Any
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
    # When True, the UI/CLI can fetch a live option list for this variable from
    # ``GET /api/tools/{tool_id}/variable-options`` (backed by the module-level
    # ``get_variable_options`` hook). On write (``PUT .../variables``) a value IS
    # validated against that live list when it resolves to a non-empty set — a
    # value outside it is rejected (HTTP 400 with the valid values) — UNLESS the
    # caller passes ``allow_unknown`` (the Web UI always does; the CLI does with
    # ``--force``). Validation is skipped when the list can't be resolved
    # (offline / no credential / backing SDK absent), so legitimate custom values
    # and offline use still work. Use this (not ``enum``) when the valid set is
    # discovered at runtime rather than fixed in the schema.
    dynamic_options: bool


class OAuthConfig(TypedDict, total=False):
    """OAuth provider endpoints and scopes."""
    authorization_endpoint: Required[str]
    token_endpoint: Required[str]
    scopes: list[str]
    extra_authorize_params: dict[str, str]


class ToolConfig(TypedDict, total=False):
    """Static configuration exported as ``TOOL_CONFIG`` by each built-in tool module."""
    name: Required[str]
    display_name: Required[str]
    visible: bool
    # When True, the tool is still registered with the agent (and remains
    # available at runtime) but is suppressed from the Settings UI and the
    # Setup Wizard catalog. Use for "system" built-ins whose lifecycle is
    # controlled by the runtime, not the user (e.g. ``change_working_directory``):
    # disabling them would break internal flows.
    hidden: bool
    required_config: dict[str, RequiredConfigField]
    oauth: OAuthConfig
    arguments: dict[str, Any]
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
    # When False, the tool starts DISABLED in the Setup Wizard's tools step
    # (its toggle is off by default; the user opts in). Defaults to True, so an
    # undeclared tool starts enabled. Built-in tools only. NOTE: this is a
    # top-level flag distinct from ``RequiredConfigField.default`` (the default
    # *value* of a required env var) — see that TypedDict above.
    default: bool


class ChatCompletionStreamResponseType(TypedDict):
    type: ChatCompletionTypeEnum
    data: Required[Optional[Any]]
    last_token: NotRequired[bool]
    # ``input_tokens`` is the UNCACHED input only. Cached prompt tokens are
    # reported separately so cost can be attributed accurately:
    #   cache_read_input_tokens     -- served from cache (heavily discounted)
    #   cache_creation_input_tokens -- written to cache (Anthropic only; premium)
    input_tokens: NotRequired[Optional[int]]
    cache_read_input_tokens: NotRequired[Optional[int]]
    cache_creation_input_tokens: NotRequired[Optional[int]]
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
    # ``input_tokens`` is uncached input only; cached tokens are reported
    # separately (see ``ChatCompletionStreamResponseType``).
    input_tokens: NotRequired[Optional[int]]
    cache_read_input_tokens: NotRequired[Optional[int]]
    cache_creation_input_tokens: NotRequired[Optional[int]]
    output_tokens: NotRequired[Optional[int]]
    # Per-source token attribution for this turn (one entry per LLM invocation:
    # reasoning step vs. specific tool/sub-agent). Carried on terminal chunks so
    # the runner can freeze cost and persist to ``usage_records``. The aggregate
    # token fields above remain authoritative for backward compatibility.
    usage_records: NotRequired[list[dict]]
