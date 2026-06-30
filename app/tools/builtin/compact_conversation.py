"""Conversation-compaction tool (model-driven).

Compaction is performed by the MAIN model: with the whole conversation already
in its cached prefix, it writes a dense running summary (and any durable
long-term facts) and passes them as this tool's arguments. The tool persists
them via :func:`app.agent.compaction.apply_compaction`, advancing the watermark
so the verbatim tail collapses on the next turn. Reusing the cached prefix is
what makes this cheap — far cheaper than a separate uncached summarizer call.

The tool is hidden from the Settings UI (it's a system capability, not a
user-managed tool) but always available to the agent. It is invoked either when
the user explicitly asks to compact, or by a synthetic "please compact" turn the
backend runs when the conversation crosses the suggestion threshold and the user
clicks Compact.
"""

from __future__ import annotations

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Compact Conversation"

TOOL_CONFIG: ToolConfig = {
    "name": "compact_conversation",
    "display_name": "Compact Conversation",
    # System capability — exposed to the agent, hidden from the Settings UI.
    "hidden": True,
}


class CompactConversationTool(BuiltInTool):
    name: str = "compact_conversation"
    description: str = (
        "Compact this conversation to free up context. Call this when the user "
        "asks to compact/summarize the conversation (or when asked to free "
        "context). Provide `summary`: a dense, self-contained running summary of "
        "EVERYTHING discussed so far that preserves all facts, decisions, "
        "identifiers (IDs, file paths, URLs, commands, config keys, exact "
        "values), unresolved questions and pending TODOs, and the user's goals, "
        "constraints and preferences — written so you could continue seamlessly "
        "with only this summary plus the most recent messages. Optionally provide "
        "`long_term_memories`: durable, session-independent facts worth "
        "remembering across future conversations."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "The dense running summary of the whole conversation so far "
                    "(preserve facts, identifiers, TODOs, goals, constraints)."
                ),
            },
            "long_term_memories": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "0+ durable, self-contained facts to remember across future "
                    "conversations. Empty if none qualify."
                ),
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        summary = str(arguments.get("summary") or "").strip()
        long_term = arguments.get("long_term_memories") or []
        if isinstance(long_term, str):
            long_term = [long_term]
        context_id = arguments.get("_context_id")
        profile = arguments.get("_profile") or arguments.get("profile") or "default"

        if not summary:
            return BuiltInToolResult(structured_content={
                "error": "EmptySummary",
                "message": "Provide a non-empty summary to compact the conversation.",
            })
        if not context_id:
            return BuiltInToolResult(structured_content={
                "error": "NoContext",
                "message": "No conversation context available to compact.",
            })

        try:
            from app.events.runner import get_conversation_storage
            from app.agent.compaction import apply_compaction

            cs = get_conversation_storage()
            conv = await cs.get_conversation_by_context(profile=profile, context_id=context_id)
            if conv is None:
                conv = await cs.get_conversation(context_id)
            if conv is None:
                return BuiltInToolResult(structured_content={
                    "error": "NoConversation",
                    "message": "Could not resolve the conversation to compact.",
                })

            result = await apply_compaction(
                conversation_id=conv["id"], profile=profile,
                summary=summary, long_term=long_term, conversation_storage=cs,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[compact_conversation] failed")
            return BuiltInToolResult(structured_content={
                "error": "CompactionFailed", "message": str(e),
            })

        return BuiltInToolResult(structured_content={
            "status": "compacted",
            "message": (
                "Conversation compacted: earlier turns are now represented by the "
                "running summary; recent messages remain verbatim."
            ),
            **result,
        })


def get_tools(config: dict) -> list[BuiltInTool]:
    return [CompactConversationTool()]
