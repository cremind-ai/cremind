"""Base classes for built-in tools.

Provides lightweight replacements for FastMCP's Tool and ToolResult so that
built-in tools can run directly in-process without MCP stdio transport.

Error contract
--------------
Every built-in tool must surface runtime errors to the Reasoning Agent as
structured observations — never swallow them with ``logger.warning`` and a
silent ``return []``/``None``. The agent reads ``BuiltInToolResult.structured_content``
as the tool-call result and decides the next action (retry, ask the user,
suggest installing a missing feature, etc.). Two shapes:

1. Generic errors: ``{"error": "<code>", "message": "<human text>", ...}``.
2. Missing optional dependency: produced by :func:`missing_dependency_result`,
   which always sets ``error="MissingDependency"`` plus the relevant
   ``feature_key``/``extras`` so the agent can point the user at the right
   Settings → Features entry.

Tool authors should keep dependency checks inside ``run()`` (so the tool is
always registered and the agent can see it). Registration-time fallbacks for
modules that fail to import or whose ``get_tools()`` raises are handled by
:class:`_StubErrorTool` in :mod:`app.tools.builtin.__init__`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class BuiltInToolResult:
    """Result from a built-in tool execution.

    Mirrors FastMCP's ToolResult interface with the same two primary fields
    so that the BuiltInToolAdapter's _extract_tool_result() works identically.

    Attributes:
        content: Optional list of content items (dicts with 'type'/'text'/'data' keys).
        structured_content: Optional dict returned as structured JSON data.
        token_usage: Optional four-way token breakdown of any internal LLM call the
            tool made (e.g. the documentation_search judge, image_understanding's
            vision call). The adapter folds this into the tool result's token_usage
            so it lands in a ``source_kind="tool"`` usage record; ``None`` for tools
            that make no LLM call.
    """
    content: Optional[List[Any]] = None
    structured_content: Optional[Dict[str, Any]] = None
    token_usage: Optional[Dict[str, int]] = None


class BuiltInTool:
    """Base class for built-in tools (replaces fastmcp.tools.tool.Tool).

    Subclasses must set ``name``, ``description``, ``parameters`` (JSON Schema)
    and implement ``async run(arguments) -> BuiltInToolResult``.
    """
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        """Execute the tool with the given arguments.

        Args:
            arguments: Tool arguments as validated by the JSON Schema in ``parameters``.

        Returns:
            BuiltInToolResult with either ``content`` or ``structured_content`` populated.
        """
        raise NotImplementedError


def missing_dependency_result(
    *,
    tool: str,
    feature_key: str,
    extras: Tuple[str, ...] = (),
    detail: str = "",
    install_commands: Tuple[str, ...] = (),
) -> BuiltInToolResult:
    """Canonical 'feature dep not installed' result.

    The Reasoning Agent reads ``structured_content`` as an observation, so
    using one shape across tools lets the agent recognise the situation and
    consistently suggest the same remediation to the user.

    Important: enabling a tool in the Tools & Skills UI only flips a
    configuration flag. The underlying Python wheels for optional features
    must be installed separately (the Setup Wizard does this at first
    install; post-setup there is no UI surface, so the user has to either
    re-run the wizard or pip-install the extras group from a shell).

    Args:
        tool: Tool name as it appears in the catalog (e.g. ``"browser"``).
        feature_key: Key from :data:`app.features.manifest.FEATURES`
            (e.g. ``"browser"``, ``"documents"``).
        extras: pyproject extras-group names the feature maps to.
        detail: Optional extra text for the agent (e.g. the underlying
            ImportError message).
        install_commands: Override the auto-generated pip command sequence.
            Use this when the feature requires post-install steps (e.g.
            ``python -m playwright install --with-deps chromium`` for
            ``browser``).
    """
    message = (
        f"The '{tool}' tool is unavailable because its Python dependencies "
        f"are not installed (feature: {feature_key})."
    )
    if detail:
        message = f"{message} {detail}"

    if install_commands:
        commands = list(install_commands)
    elif extras:
        commands = [f"pip install 'cremind[{','.join(extras)}]'"]
    else:
        commands = []

    remediation_lines = [
        (
            "Enabling the tool in the Tools & Skills UI does not install "
            "its Python dependencies. To enable this tool, install the "
            f"'{feature_key}' feature from a shell and restart the server:"
        ),
    ]
    remediation_lines.extend(f"  {c}" for c in commands)
    remediation_lines.append(
        "Then stop and restart the Cremind server process."
    )

    return BuiltInToolResult(structured_content={
        "error": "MissingDependency",
        "tool": tool,
        "feature_key": feature_key,
        "extras": list(extras),
        "install_commands": commands,
        "message": message,
        "remediation": "\n".join(remediation_lines),
    })


def restart_required_result(
    *,
    tool: str,
    feature_key: str,
    detail: str = "",
) -> BuiltInToolResult:
    """Canonical 'feature installed but server not restarted' result.

    Returned when a tool's runtime gate sees that its optional pip
    extras are on disk *now* but were not importable when the running
    process started. The Browser tool is the canonical case: its
    ``_PLAYWRIGHT_AVAILABLE`` flag and the ``Browser``/``Page``/...
    type names are bound at module-load time. After a pip install in a
    running session, the wheel is present but the running process
    can't see the new symbols without a restart.

    This is distinct from :func:`missing_dependency_result`:

    * ``MissingDependency`` — the wheel is not on disk; user must pip
      install.
    * ``RestartRequired`` — the wheel is on disk; user must restart
      the server to load it.

    Giving the agent the right error means the user receives the right
    remediation instead of a confusing "playwright not installed"
    message when they just ran ``pip install``.
    """
    message = (
        f"The '{tool}' tool is installed on disk but the running "
        "Cremind server cannot load it without a restart."
    )
    if detail:
        message = f"{message} {detail}"
    return BuiltInToolResult(structured_content={
        "error": "RestartRequired",
        "tool": tool,
        "feature_key": feature_key,
        "message": message,
        "remediation": (
            "Stop and restart the Cremind server process to load the "
            f"'{feature_key}' feature. Some optional features (e.g. "
            "Playwright) initialize at module import time, so a "
            "fresh process is required after a pip install."
        ),
    })


@dataclass
class _StubErrorPayload:
    """Static error info captured at registration time for :class:`_StubErrorTool`."""

    kind: str           # e.g. "ImportError", "FactoryError"
    detail: str         # underlying exception text
    extra: Dict[str, Any] = field(default_factory=dict)


class _StubErrorTool(BuiltInTool):
    """Placeholder tool registered when a built-in module fails to load.

    Lives at registration time only — when ``importlib.import_module`` raises
    or a module's ``get_tools()`` factory raises / returns ``[]``, the
    registration loop substitutes one of these so the tool group still
    appears in the registry and any invocation reaches the agent as a
    structured error rather than a confusing "no such tool" miss.
    """

    parameters: Dict[str, Any] = {
        "type": "object",
        # Permissive: we don't know the real schema (the factory failed
        # before we could ask it), so accept anything and surface the
        # captured failure on invocation.
        "additionalProperties": True,
    }

    def __init__(self, *, server_name: str, payload: _StubErrorPayload):
        self._server_name = server_name
        self._payload = payload
        # Mirror the failed group's name so the agent's catalog shows the
        # tool the user expects, just in a degraded state.
        self.name = server_name
        self.description = (
            f"[unavailable] The '{server_name}' tool failed to register "
            f"({payload.kind}). Calling it returns the captured error."
        )

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return BuiltInToolResult(structured_content={
            "error": self._payload.kind,
            "tool": self._server_name,
            "message": (
                f"The '{self._server_name}' tool failed to register at "
                f"server boot ({self._payload.kind})."
            ),
            "detail": self._payload.detail,
            "remediation": (
                "Check the server logs for the underlying error and "
                "restart the server once it has been resolved."
            ),
            **self._payload.extra,
        })
