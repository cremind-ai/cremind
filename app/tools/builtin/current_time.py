"""Current-time built-in tool.

Reports the current wall-clock time, optionally in a caller-specified IANA
timezone, so the agent can answer "what time is it" and "what time is it in
Tokyo right now".

This replaces the per-turn ``Current time:`` line that used to live in the
reasoning system prompt. That line mutated the cached system prefix on every
turn, forcing a fresh prompt-cache write each turn; moving the clock into an
on-demand tool keeps the system prompt byte-stable across turns.

Like :mod:`app.tools.builtin.datetime_parser`, the child LLM fills the
structured ``timezone`` argument (mapping a named location such as "Tokyo" to
its IANA zone ``Asia/Tokyo``); the actual clock read is pure Python. Timezones
resolve via the stdlib :mod:`zoneinfo` (``tzdata`` ships as a dependency on
Windows), so no extra feature gate is required.
"""

from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Current Time"


TOOL_CONFIG: ToolConfig = {
    "name": "current_time",
    "display_name": "Current Time",
    # Lightweight, structured extraction — matches every other extraction tool.
    "default_model_group": "low",
    # NOT hidden: the reasoning agent must see this tool to answer time
    # questions. The reasoning model fills the structured ``timezone``
    # argument directly (resolving a named location to its IANA zone).
    "llm_parameters": {
        "tool_instructions": (
            "Get the current date and time, optionally in a specific timezone."
        ),
    },
}


def _format_offset(now: datetime) -> str:
    """Render the UTC offset as ``+HH:MM`` (or ``unknown`` if naive)."""
    raw = now.strftime("%z")
    return f"{raw[:3]}:{raw[3:]}" if raw else "unknown"


class GetCurrentTimeTool(BuiltInTool):
    name: str = "get_current_time"
    description: str = (
        "Return the current date and time. With no timezone, returns the "
        "server's local time. Pass an IANA timezone name to get the current "
        "time there (e.g. 'Asia/Tokyo' for Tokyo)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": (
                    "Optional IANA timezone name, e.g. 'Asia/Tokyo', "
                    "'America/New_York', 'Europe/London', 'UTC'. If the user "
                    "names a city or region, map it to its IANA zone. Leave "
                    "empty for the server's local time."
                ),
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        logger.info(f"[current_time] Received arguments: {arguments}")
        tz_name = str(arguments.get("timezone") or "").strip()

        # ``_now`` is a test/caller override (matches the ``_``-prefixed
        # injected-key convention used by datetime_parser). Expect an ISO string.
        override = arguments.get("_now")
        try:
            base = datetime.fromisoformat(override) if override else None
        except (TypeError, ValueError):
            base = None

        if tz_name:
            try:
                tzinfo = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError):
                # Surface a structured error rather than silently reporting
                # server-local time as if it were the requested zone.
                return BuiltInToolResult(
                    structured_content={
                        "error": "InvalidTimezone",
                        "requested": tz_name,
                        "message": (
                            f"'{tz_name}' is not a valid IANA timezone name. Use "
                            "names like 'Asia/Tokyo', 'America/New_York', or 'UTC'."
                        ),
                    }
                )
            now = base.astimezone(tzinfo) if base else datetime.now(tzinfo)
            tz_label = tz_name
        else:
            now = (base if base else datetime.now()).astimezone()
            tz_label = now.tzname() or "local"

        response_data = {
            "iso": now.isoformat(timespec="seconds"),
            "human_readable": now.strftime("%A, %d %B %Y, %H:%M:%S"),
            "timezone": tz_label,
            "utc_offset": _format_offset(now),
        }
        logger.info(f"[current_time] Result: {response_data}")
        return BuiltInToolResult(structured_content=response_data)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [GetCurrentTimeTool()]
