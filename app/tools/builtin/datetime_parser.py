"""Datetime Parser built-in tool.

Decomposes a natural-language time expression into atomic time offsets (the
LLM's job, via the tool-call schema) and computes the concrete datetime(s) in
pure Python (via :mod:`app.utils.datetime`). Other tools (calendar, reminders)
then receive precise, machine-usable datetimes instead of ambiguous text.

Ported from the ``a2a-datetime-parser-agent`` project. The schema is kept
identical to that project so the LLM's reasoning stays consistent, with ONE
addition: a ``single_time_mode`` property the LLM sets per-command (a2a decided
this from a startup flag instead).

The returned datetimes are naive local wall-clock strings
(``YYYY-MM-DDTHH:MM:SS``); the consuming tool owns timezone localization.
"""

from datetime import datetime
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.datetime import (
    convert_datetime_payload,
    build_payload_from_elements,
)


SERVER_NAME = "Datetime Parser"


TOOL_CONFIG: ToolConfig = {
    "name": "datetime_parser",
    "display_name": "Datetime Parser",
    # Lightweight, structured extraction — matches every other extraction tool
    # (register_skill_event, documentation_search, weather all use "low").
    "default_model_group": "low",
    "hidden": True,
    # NOTE: must NOT be direct_dispatch. Direct dispatch bypasses the routing
    # LLM and feeds {"query": <text>} verbatim, but this tool's entire contract
    # is that the LLM fills the structured schema below.
    "llm_parameters": {
        "tool_instructions": (
            "Decompose time expressions in a sentence into structured, computed datetime(s)."
        ),
        "system_prompt": (
            "You decompose a natural-language time expression into atomic time "
            "offsets and call the datetime_parser tool with the result. You have "
            "exactly one task.\n"
            "NEVER use your own knowledge of the current date or time to compute "
            "anything — emit only offsets and let the system compute the actual "
            "datetime. For example: 'last year' is a relative year offset of -1; "
            "'tomorrow' is a relative day offset of 1; '3pm' is an absolute hour "
            "of 15; 'next Monday' is offset_unit='monday' with offset_value=1.\n"
            "Decompose each time mention left-to-right into one time_elements "
            "entry per atomic unit. For an explicit range ('from X to Y'), tag "
            "the start units with time_range='start' and the end units with "
            "time_range='end'; otherwise use 'start'. Set single_time_mode=true "
            "for a single precise instant, false for a period to expand into a "
            "range.\n"
            "Do not answer questions or perform any other task. Always respond by "
            "calling datetime_parser."
        ),
    },
}


class DatetimeParserTool(BuiltInTool):
    name: str = "datetime_parser"
    description: str = (
        "Decompose time spans within a sentence into a list of independent atomic time elements."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Think carefully and provide an analysis of why you're using these time parameters to generate the results."
            },
            "parsable": {
                "type": "boolean",
                "description": "Indicates whether the datetime information could be parsed from the input."
            },
            "time_elements": {
                "type": "array",
                "description": "Ordered list of atomic time components (left-to-right order in the sentence). Each object contains exactly one time-unit key that it refers to in the user's command; for example, if the time is mentioned as a day, the offset_unit must be day, if it's an hour, the offset_unit must be hour, and similarly for other units.",
                "items": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["absolute", "relative"]
                        },
                        "time_range": {
                            "type": "string",
                            "description": "Indicates whether this time element represents the start or end of a time range. For time ranges (e.g., 'from 2pm to 4pm'), the start time would have 'time_range': 'start' and the end time would have 'time_range': 'end'. For single time points (e.g., 'tomorrow at 3pm'), this field can be set to 'start' or omitted based on your preference, but for consistency, you can treat single time points as having 'time_range': 'start'.",
                            "enum": ["start", "end"]
                        },
                        "offset_unit": {
                            "type": "string",
                            "enum": ["year", "month", "day", "hour", "minute", "second", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
                        },
                        "offset_value": {
                            "type": "integer",
                            "description": "For relative times, the integer offset (e.g., day=0 for 'today', day=1 for 'tomorrow', day=-1 for 'yesterday', month=1 for 'next month', year=-1 for 'last year', hour=-1 for 'last hour', etc.). For absolute times, the concrete value (e.g., month=4 for April). For weekdays, use offset_unit for the day and offset_value for the occurrence (e.g., offset_unit='monday', offset_value=0 for 'this Monday', offset_value=1 for 'next Monday', offset_value=-1 for 'last Monday', etc.)."
                        }
                    },
                    "required": ["mode", "time_range", "offset_unit", "offset_value"],
                    "additionalProperties": False
                }
            },
            "components_count": {
                "type": "integer",
                "description": "Exact length of the time_elements array"
            },
            "single_time_mode": {
                "type": "boolean",
                "description": (
                    "Set true when the command denotes a single precise instant and the caller "
                    "wants exactly one datetime (e.g. 'tomorrow at 3pm', 'in two hours', 'now'). "
                    "Set false when it denotes a period that should expand into a start/end pair "
                    "covering the whole unit (e.g. 'today' -> 00:00:00..23:59:59, 'next month' -> "
                    "first..last day, '2025' -> Jan 1..Dec 31). Only affects single time points; "
                    "ignored when the sentence already gives an explicit start and end. When "
                    "unsure, prefer true."
                )
            },
        },
        "required": ["reasoning", "parsable", "time_elements", "components_count"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        logger.info(f"[datetime_parser] Received arguments: {arguments}")

        parsable = arguments.get("parsable", False)
        time_elements = arguments.get("time_elements", [])

        # Early exit if not parsable or no elements. Surface a structured
        # observation (not a text content block) so the reasoning agent reads a
        # uniform shape.
        if not parsable or not time_elements:
            return BuiltInToolResult(
                structured_content={
                    "parsable": False,
                    "reason": arguments.get("reasoning") or "Could not parse datetime from input",
                }
            )

        # ── Build the time payload from the atomic elements ──
        # Partitioning by time_range, date inheritance and component building
        # all live in app.utils.datetime so the scheduler reuses the exact
        # same logic per anchor.
        payload = build_payload_from_elements(time_elements)

        # Current datetime as ISO string. ``_now`` is a test/caller override
        # (matches the ``_``-prefixed injected-key convention); otherwise use
        # the naive local wall-clock, as the source library expects.
        current_date_str = arguments.get("_now") or datetime.now().isoformat()

        # single_time_mode is detected by the LLM per-command (default True).
        single_time_mode = arguments.get("single_time_mode", True)
        logger.debug(f"[datetime_parser] single_time_mode={single_time_mode}")

        try:
            result = convert_datetime_payload(payload, current_date_str, single_time_mode)
        except Exception as e:
            # The source library can raise on day-overflow shifts (e.g. Jan 31 +
            # 1 month). Surface it as a structured observation, never a crash.
            logger.warning(f"[datetime_parser] conversion failed: {e}")
            return BuiltInToolResult(
                structured_content={
                    "error": "ConversionError",
                    "parsable": False,
                    "message": f"Failed to compute datetime: {e}",
                }
            )

        # ── Format the structured observation (uniform shape, always parsable) ──
        response_data: Dict[str, Any] = {"parsable": result.parsable}

        if result.reason:
            response_data["reason"] = result.reason

        if result.time_single:
            response_data["time_single"] = result.time_single.model_dump(exclude_none=True)
        elif result.time_range:
            response_data["time_range"] = {
                "start_date": result.time_range["start_date"].model_dump(exclude_none=True),
                "end_date": result.time_range["end_date"].model_dump(exclude_none=True),
            }

        logger.info(f"[datetime_parser] Converted result: {response_data}")

        return BuiltInToolResult(structured_content=response_data)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [DatetimeParserTool()]
