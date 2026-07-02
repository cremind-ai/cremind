"""CLI built-in tool.

The agent's gateway to operating Cremind through its command-line interface.
Mechanically it is a documentation search — it vector-searches the bundled
``cremind`` CLI usage docs (the ``cli`` scope, kept disjoint from the general
``documentation_search`` corpus) and runs the same internal LLM-as-judge to
return the single most relevant CLI reference. The reasoning model is then
expected to actually *run* the commands via ``exec_shell``.

All of the search/judge machinery is shared with ``documentation_search`` via
:func:`app.tools.builtin.documentation_search.run_doc_search`; this module only
narrows the corpus to :data:`app.documents.sync.CLI_SCOPE` and frames the tool
as a system-interaction surface rather than a passive doc lookup.
"""

from __future__ import annotations

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig


SERVER_NAME = "CLI"

DEFAULT_TOP_K = 10


class Var:
    DEFAULT_TOP_K_KEY = "DEFAULT_TOP_K"


TOOL_CONFIG: ToolConfig = {
    "name": "cli",
    "display_name": SERVER_NAME,
    # Locked on — the CLI is a core capability the agent must always be able
    # to reach (mirrors documentation_search).
    "locked": True,
    "required_config": {
        Var.DEFAULT_TOP_K_KEY: {
            "description": (
                "Maximum number of CLI documents the vector store returns to "
                "the relevance judge for each search call."
            ),
            "type": "number",
            "default": DEFAULT_TOP_K,
        },
    },
}


class CliTool(BuiltInTool):
    name: str = "cli"
    description: str = (
        "Operate the Cremind system through its command-line interface. The "
        "cremind CLI is a very powerful surface that can do anything a user can "
        "do in the UI: install and manage skills & tools, change configuration, "
        "and manage profiles, conversations, files, calendar, channels, usage "
        "and more. Given a natural-language goal, this returns the exact CLI "
        "usage instructions for it. You MUST then run the relevant commands with "
        "the `exec_shell` tool to actually perform the action — do not just show "
        "the instructions for the user to run themselves."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language description of what the user wants to do "
                    "with the system via the CLI. Example: 'create a new "
                    "profile' or 'list the configured tools'."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": (
                    "Maximum number of vector-search candidates the LLM judge "
                    "considers. Defaults to 10 and is capped at 20."
                ),
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        # Reuse documentation_search's full search + LLM-judge pipeline, but
        # restricted to the CLI-docs scope so the two corpora stay disjoint.
        from app.documents.sync import CLI_SCOPE
        from app.tools.builtin.documentation_search import run_doc_search

        return await run_doc_search(arguments, scopes=[CLI_SCOPE], log_label="cli")


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [CliTool()]
