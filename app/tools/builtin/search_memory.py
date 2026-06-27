"""Semantic long-term memory search tool.

Long-term memory (durable facts, preferences, project/environment details the
user wants remembered across conversations) is NO LONGER injected into every
prompt — that mutated the volatile input each turn and gave the model facts it
rarely needed. Instead the model decides when to look, by calling this tool.

Retrieval uses the vector store when embedding is enabled (top-K similarity to
the query) and the DB queue otherwise. Writing happens via the
``compact_conversation`` tool (see :mod:`app.agent.compaction`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Memory Search"

TOOL_CONFIG: ToolConfig = {
    "name": "search_memory",
    "display_name": "Memory Search",
    "description": (
        "Search the user's long-term memory for durable facts, preferences, and "
        "project details remembered across conversations."
    ),
}

_DEFAULT_LIMIT = 10


class SearchMemoryTool(BuiltInTool):
    name: str = "search_memory"
    description: str = (
        "Search the user's long-term memory — durable facts, preferences, and "
        "project/environment details remembered across past conversations. Call "
        "this when answering would benefit from background about the user that is "
        "NOT already in the current conversation (e.g. their name, stable "
        "preferences, where things live, how they like work done). Returns the "
        "matching remembered facts, or nothing if memory is empty."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up in long-term memory.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        query = str(arguments.get("query") or "").strip()
        profile = arguments.get("_profile") or arguments.get("profile") or "default"
        if not query:
            return BuiltInToolResult(structured_content={"memories": [], "count": 0})

        facts = await self._retrieve(profile, query)
        if not facts:
            return BuiltInToolResult(structured_content={
                "count": 0,
                "memories": [],
                "message": "No stored long-term memory matched this query.",
            })
        return BuiltInToolResult(structured_content={"count": len(facts), "memories": facts})

    async def _retrieve(self, profile: str, query: str) -> list[str]:
        from app.config.embedding_state import embedding_state
        from app.agent import memory_vectorstore

        shim = SimpleNamespace(
            embedding=embedding_state.embedding, vector_store=embedding_state.vector_store,
        )
        if memory_vectorstore.vector_long_term_available(shim):
            rows = memory_vectorstore.retrieve_long_term(
                agent=shim, profile=profile, query_text=query, limit=_DEFAULT_LIMIT,
            )
            return [r["content"] for r in rows if r.get("content")]

        try:
            from app.storage import get_memory_storage
            rows = await get_memory_storage().get_long_term(profile)
            return [r["content"] for r in rows if r.get("content")]
        except Exception:  # noqa: BLE001
            logger.exception("[search_memory] DB get_long_term failed")
            return []


def get_tools(config: dict) -> list[BuiltInTool]:
    return [SearchMemoryTool()]
