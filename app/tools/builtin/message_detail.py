"""Message Detail built-in tool.

Retrieves full details of conversation messages by their IDs from the active
database provider.
"""

import json
from typing import Any, Dict

from sqlalchemy import bindparam, text

from app.databases import get_database_provider
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig


SERVER_NAME = "Message Detail"

TOOL_CONFIG: ToolConfig = {
    "name": "message_detail",
    "display_name": "Message Detail",
    "default_model_group": "low",
    "visible": False,
    "llm_parameters": {
        "tool_instructions": "Retrieve the full details of conversation messages by their IDs.",
    },
}


def _parse_json_column(value: Any) -> Any:
    """Parse a JSON column value that may be a string or already decoded.

    SQLite stores JSON columns as TEXT, so we may receive strings; Postgres
    JSONB returns dicts/lists already-decoded. Handle both transparently.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


class GetMessageDetailTool(BuiltInTool):
    name: str = "get_message_detail"
    description: str = (
        "This tool is used to extract the full details of a message, including message parts, thinking steps, and metadata. "
        "E.g. 'get details for <message_id>', 'show details steps reasoning for <message_id>'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of message IDs (UUIDs) to retrieve.",
            }
        },
        "required": ["message_ids"],
        "additionalProperties": False,
    }

    def __init__(self, **_kwargs: Any):
        # Backend resolved from the active DatabaseProvider — no per-tool path
        # configuration. Old keyword args (e.g. ``db_path``) are accepted and
        # ignored so the registry doesn't need to know which tools migrated.
        pass

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        message_ids = arguments.get("message_ids", [])
        if not message_ids:
            return BuiltInToolResult(
                structured_content={"error": "No message IDs provided."}
            )

        try:
            engine = get_database_provider().sync_engine()
            with engine.connect() as conn:
                # Use SQLAlchemy's expanding bindparam so the IN-list works
                # across both SQLite (?) and Postgres (%s) parameter styles.
                stmt = text(
                    "SELECT id, role, content, parts, thinking_steps, "
                    "metadata, created_at, ordering "
                    "FROM messages WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                rows = conn.execute(stmt, {"ids": list(message_ids)}).mappings().fetchall()

            messages = []
            for row in rows:
                msg: Dict[str, Any] = {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "ordering": row["ordering"],
                }
                for json_col in ("parts", "thinking_steps", "metadata"):
                    parsed = _parse_json_column(row[json_col])
                    if parsed is not None:
                        msg[json_col] = parsed
                messages.append(msg)

            # Preserve the requested order
            id_order = {mid: i for i, mid in enumerate(message_ids)}
            messages.sort(key=lambda m: id_order.get(m["id"], len(message_ids)))

            found_ids = {m["id"] for m in messages}
            not_found = [mid for mid in message_ids if mid not in found_ids]

            result: Dict[str, Any] = {"messages": messages}
            if not_found:
                result["not_found"] = not_found

            return BuiltInToolResult(structured_content=result)

        except Exception as e:
            return BuiltInToolResult(
                structured_content={"error": f"Database query failed: {str(e)}"}
            )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [GetMessageDetailTool()]
