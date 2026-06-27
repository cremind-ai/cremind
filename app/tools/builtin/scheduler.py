"""Scheduler built-in tool.

Sibling of the ``datetime_parser`` tool. Where that tool normalizes a single
time expression into concrete datetime(s), this tool classifies a *schedule*
expression into one of six core data types and normalizes it into a structured,
machine-usable result for downstream schedule services (the Cremind internal
calendar, Google Calendar, iCloud/CalDAV, Reminders):

* **instant**      — a single point ("at 9 AM")
* **interval**     — a bounded span, start+end or start+duration ("2-4 PM")
* **recurrence**   — a generator rule ("every weekday at 9")
* **explicit_set** — a hand-listed union ("July 3 and July 5 at 2pm")
* **window**       — a query region, not a booking ("this week")
* **constraint**   — a predicate qualifying any of the above ("weekday
  afternoons only")

Like ``datetime_parser``, the LLM never computes dates: it decomposes each time
anchor into atomic offsets (the reused ``time_elements`` schema below) and the
concrete datetimes are computed in pure Python — here via
:mod:`app.utils.schedule`, which composes :mod:`app.utils.datetime`. For
recurrences the tool also emits an RFC 5545 RRULE string. Returned datetimes are
naive local wall-clock (``YYYY-MM-DDTHH:MM:SS``); the consuming tool owns
timezone localization (signalled by ``timezone: "pending"`` in the output).

This module is the home of the **scheduler** tool group. The single ``scheduler``
function below (the schedule parser / normalizer) is always exposed. When the
per-profile *Calendar & Schedule* feature is enabled, this group additionally
exposes action subtools (create / list / update / cancel schedule events) — see
:mod:`app.tools.builtin.scheduler_actions` and the ``get_prepare_tools`` gate.
"""

from datetime import datetime
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.schedule import compute_schedule


SERVER_NAME = "Scheduler"


# Atomic time-element item schema — copied verbatim from datetime_parser so the
# LLM's time-decomposition contract is identical everywhere a time appears.
_TIME_ELEMENT_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["absolute", "relative"],
        },
        "time_range": {
            "type": "string",
            "description": (
                "Whether this element is the start or end of a span. For an "
                "explicit interval ('from 2pm to 4pm') tag start units 'start' "
                "and end units 'end'. For a single point use 'start'."
            ),
            "enum": ["start", "end"],
        },
        "offset_unit": {
            "type": "string",
            "enum": [
                "year", "month", "day", "hour", "minute", "second",
                "sunday", "monday", "tuesday", "wednesday",
                "thursday", "friday", "saturday",
            ],
        },
        "offset_value": {
            "type": "integer",
            "description": (
                "For relative times, the integer offset (day=0 today, day=1 "
                "tomorrow, day=-1 yesterday, month=1 next month, year=-1 last "
                "year, hour=-1 last hour). For absolute times, the concrete "
                "value (month=4 for April, hour=15 for 3pm). For weekdays, "
                "offset_unit is the weekday and offset_value is the occurrence "
                "(0='this', 1='next', -1='last')."
            ),
        },
    },
    "required": ["mode", "time_range", "offset_unit", "offset_value"],
    "additionalProperties": False,
}

_DURATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "A length of time, for an interval given as start + duration ('for 2 "
        "hours', '90-minute meeting'). Set only the fields mentioned. Omit "
        "entirely when an explicit end clock time is spoken (use an 'end' "
        "time_elements entry instead). No months/years — express those via end "
        "time_elements."
    ),
    "properties": {
        "days": {"type": "integer"},
        "hours": {"type": "integer"},
        "minutes": {"type": "integer"},
        "seconds": {"type": "integer"},
    },
    "additionalProperties": False,
}


TOOL_CONFIG: ToolConfig = {
    "name": "scheduler",
    "display_name": "Scheduler",
    # Lightweight structured extraction, like datetime_parser and the other
    # extraction tools.
    "default_model_group": "low",
    "hidden": True,
    # This tool's contract is that the reasoning model fills the structured
    # schema below directly via native function calling.
    "llm_parameters": {
        "tool_instructions": (
            "Classify and normalize a schedule expression (a single time, an "
            "interval, a recurring rule, a hand-listed set, a query window, or "
            "a filtering constraint) into machine-usable datetimes and, for "
            "recurrences, an RFC 5545 RRULE."
        ),
    },
}


class SchedulerTool(BuiltInTool):
    name: str = "scheduler"
    description: str = (
        "Classify a schedule expression (instant, interval, recurrence, "
        "explicit set, query window, or constraint) and normalize it into "
        "concrete datetimes and an RFC 5545 RRULE for calendar/reminder services. "
        "Decompose each time anchor into atomic relative offsets — do NOT compute "
        "absolute datetimes from your own knowledge of the current time; the "
        "server resolves the offsets against the real clock. To then create the "
        "scheduled task, pass the normalized output to schedule_create."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": (
                    "Think step by step. State which schedule_kind the phrase is "
                    "and why, then how you decomposed the time(s) into atomic "
                    "offsets. Resolve any instant-vs-interval, interval-vs-window, "
                    "or recurrence-vs-explicit_set ambiguity here first."
                ),
            },
            "parsable": {
                "type": "boolean",
                "description": (
                    "True if a schedule structure could be extracted; false if "
                    "the text contains no usable schedule/time information."
                ),
            },
            "schedule_kind": {
                "type": "string",
                "enum": [
                    "instant", "interval", "recurrence",
                    "explicit_set", "window", "constraint",
                ],
                "description": (
                    "The PRIMARY schedule structure. Pick exactly one:\n"
                    "- 'instant': a single point used as a start ('at 9 AM', "
                    "'tomorrow at noon').\n"
                    "- 'interval': one bounded span with start+end or "
                    "start+duration, intent to BOOK ('2-4 PM', 'meet for 2 "
                    "hours at 1').\n"
                    "- 'recurrence': a rule-based repeat ('every weekday at 9', "
                    "'weekly on Tuesdays', 'first Monday of each month'). Use "
                    "this whenever the phrase implies an open-ended/rule-based "
                    "repeat.\n"
                    "- 'explicit_set': a finite hand-listed union of distinct "
                    "non-periodic points/spans ('July 3 and July 5 at 2pm', "
                    "'9am and 5pm'). If the listed items are the same weekday "
                    "repeating weekly, prefer 'recurrence' with by_weekday.\n"
                    "- 'window': a region to QUERY/search, not book ('this "
                    "week', 'my availability this afternoon', 'between today and "
                    "Friday').\n"
                    "- 'constraint': the phrase is PRIMARILY a standalone "
                    "filtering predicate ('weekday afternoons only', 'business "
                    "hours'). A predicate that qualifies another kind goes in "
                    "the 'constraints' array instead."
                ),
            },
            "time_elements": {
                "type": "array",
                "description": (
                    "Atomic time components for the ANCHOR / base time of this "
                    "schedule, decomposed left-to-right as in datetime_parser. "
                    "Role by kind: instant=the point; interval=the start (plus "
                    "'end' elements if an explicit end time is spoken); "
                    "recurrence=the time-of-day of each occurrence; window=the "
                    "queried region (usually single_time_mode=false); "
                    "explicit_set=a time shared by all members (or empty); "
                    "constraint=optional base being qualified (often empty). May "
                    "be empty for a pure recurrence or constraint."
                ),
                "items": _TIME_ELEMENT_ITEM_SCHEMA,
            },
            "components_count": {
                "type": "integer",
                "description": "Exact length of the top-level time_elements array.",
            },
            "single_time_mode": {
                "type": "boolean",
                "description": (
                    "Governs only the top-level time_elements anchor. True = a "
                    "single precise instant (instant kind, recurrence "
                    "time-of-day, interval start). False = expand the anchor to "
                    "a start/end pair covering the whole unit ('this week', "
                    "'today' -> full day) — typical for 'window'. Ignored when "
                    "time_elements already has explicit 'end' tags. Prefer true "
                    "in general; prefer false for 'window'."
                ),
            },
            "duration": _DURATION_SCHEMA,
            "recurrence": {
                "type": "object",
                "description": (
                    "For schedule_kind='recurrence'. A flat RFC 5545-aligned "
                    "rule. The time-of-day lives in time_elements, not here. "
                    "Set frequency and interval; fill by_* only for the units "
                    "the phrase constrains; set at most one of count / "
                    "until_elements."
                ),
                "properties": {
                    "frequency": {
                        "type": "string",
                        "enum": [
                            "secondly", "minutely", "hourly",
                            "daily", "weekly", "monthly", "yearly",
                        ],
                        "description": (
                            "Base repeat unit. 'every weekday'/'daily'->daily; "
                            "'weekly'/'every Monday'->weekly; 'monthly'/'first "
                            "Monday of the month'->monthly; 'every year'->yearly."
                        ),
                    },
                    "interval": {
                        "type": "integer",
                        "description": (
                            "Step between occurrences. 1 for 'every'/'each'; 2 "
                            "for 'every other week'/'biweekly'. Default 1."
                        ),
                    },
                    "by_weekday": {
                        "type": "array",
                        "description": (
                            "Weekdays the rule fires on (BYDAY), two-letter "
                            "codes. 'every weekday'->Mon-Fri; 'Mondays and "
                            "Thursdays' (recurring)->['MO','TH']."
                        ),
                        "items": {
                            "type": "string",
                            "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                        },
                    },
                    "by_monthday": {
                        "type": "array",
                        "description": (
                            "Days of the month (BYMONTHDAY), 1..31 or negative "
                            "from end (-1=last day). 'on the 1st and 15th'->"
                            "[1,15]."
                        ),
                        "items": {"type": "integer"},
                    },
                    "by_month": {
                        "type": "array",
                        "description": "Months (BYMONTH), 1..12. 'every Jan and Jul'->[1,7].",
                        "items": {"type": "integer"},
                    },
                    "by_setpos": {
                        "type": "array",
                        "description": (
                            "Nth-occurrence selector (BYSETPOS) used with "
                            "by_weekday for ordinals: 'first Monday of each "
                            "month'->by_weekday=['MO'], by_setpos=[1]; 'last "
                            "Friday'->[-1]."
                        ),
                        "items": {"type": "integer"},
                    },
                    "by_hour": {
                        "type": "array",
                        "description": (
                            "Hours (BYHOUR), 0..23. Use ONLY for several fixed "
                            "times per period ('at 9 and 17'->[9,17]); for a "
                            "single time-of-day use time_elements instead."
                        ),
                        "items": {"type": "integer"},
                    },
                    "by_minute": {
                        "type": "array",
                        "description": "Minutes (BYMINUTE), 0..59. Rarely needed; pair with by_hour.",
                        "items": {"type": "integer"},
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "Total occurrences if the phrase caps it ('the next "
                            "5 Mondays'->5). Omit if open-ended or bounded by a "
                            "date (use until_elements)."
                        ),
                    },
                    "until_elements": {
                        "type": "array",
                        "description": (
                            "End-of-series date bound as atomic offsets (same "
                            "format as time_elements). 'every day until Friday' "
                            "-> one weekday element friday/1. Omit when count is "
                            "set or the series is open-ended."
                        ),
                        "items": _TIME_ELEMENT_ITEM_SCHEMA,
                    },
                },
                "required": ["frequency"],
                "additionalProperties": False,
            },
            "members": {
                "type": "array",
                "description": (
                    "For schedule_kind='explicit_set'. A flat, one-level list of "
                    "the hand-listed items (no nested schedules). Each member "
                    "carries its own atomic decomposition; a time shared by all "
                    "members may instead be placed in the top-level "
                    "time_elements and inherited."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "member_kind": {
                            "type": "string",
                            "enum": ["instant", "interval"],
                            "description": (
                                "'instant' for a single point ('9am'); "
                                "'interval' for a span ('2-3pm on Friday')."
                            ),
                        },
                        "time_elements": {
                            "type": "array",
                            "description": (
                                "This member's atomic time, same format as the "
                                "top-level field. For an interval member tag "
                                "start/end units; omit units inherited from the "
                                "top-level shared time."
                            ),
                            "items": _TIME_ELEMENT_ITEM_SCHEMA,
                        },
                        "duration": _DURATION_SCHEMA,
                    },
                    "required": ["member_kind", "time_elements"],
                    "additionalProperties": False,
                },
            },
            "constraints": {
                "type": "array",
                "description": (
                    "Predicate filters that qualify the schedule. May co-occur "
                    "with ANY schedule_kind and is the main payload when "
                    "schedule_kind='constraint'. Each entry is one predicate "
                    "(combined as logical AND); fill only the sub-fields "
                    "relevant to its 'type'."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "weekday_membership", "time_of_day_band",
                                "date_range", "excluded_dates",
                            ],
                            "description": (
                                "'weekday_membership': restrict to weekdays "
                                "(use 'weekdays'). 'time_of_day_band': restrict "
                                "to a part of the day (use 'band' or "
                                "start_hour/end_hour). 'date_range': restrict to "
                                "a date span (use range_*_elements). "
                                "'excluded_dates': drop specific dates/days (use "
                                "excluded_elements and/or 'weekdays')."
                            ),
                        },
                        "weekdays": {
                            "type": "array",
                            "description": (
                                "Two-letter weekday codes — the ALLOWED days for "
                                "'weekday_membership' or the BLOCKED days for "
                                "'excluded_dates'. 'weekdays only'->"
                                "['MO','TU','WE','TH','FR']."
                            ),
                            "items": {
                                "type": "string",
                                "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                            },
                        },
                        "band": {
                            "type": "string",
                            "enum": [
                                "morning", "afternoon", "evening",
                                "night", "business_hours", "custom",
                            ],
                            "description": (
                                "Named time-of-day band for 'time_of_day_band'. "
                                "Use 'custom' with start_hour/end_hour for "
                                "explicit hours ('9-5')."
                            ),
                        },
                        "start_hour": {
                            "type": "integer",
                            "description": "Band lower bound 0..23 for band='custom' ('9-5'->9).",
                        },
                        "end_hour": {
                            "type": "integer",
                            "description": "Band upper bound 0..23 for band='custom' ('9-5'->17).",
                        },
                        "range_start_elements": {
                            "type": "array",
                            "description": (
                                "For 'date_range': the lower date bound as atomic "
                                "offsets (same format as time_elements)."
                            ),
                            "items": _TIME_ELEMENT_ITEM_SCHEMA,
                        },
                        "range_end_elements": {
                            "type": "array",
                            "description": (
                                "For 'date_range': the upper date bound as atomic "
                                "offsets."
                            ),
                            "items": _TIME_ELEMENT_ITEM_SCHEMA,
                        },
                        "excluded_elements": {
                            "type": "array",
                            "description": (
                                "For 'excluded_dates': a specific date to drop, "
                                "as atomic offsets. For excluded WEEKDAYS use "
                                "'weekdays' instead."
                            ),
                            "items": _TIME_ELEMENT_ITEM_SCHEMA,
                        },
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reasoning", "parsable", "schedule_kind"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        logger.info(f"[scheduler] Received arguments: {arguments}")

        parsable = arguments.get("parsable", False)
        schedule_kind = arguments.get("schedule_kind")

        # Early exit if not parsable or no kind. Uniform structured shape.
        if not parsable or not schedule_kind:
            return BuiltInToolResult(
                structured_content={
                    "parsable": False,
                    "reason": arguments.get("reasoning") or "Could not parse a schedule from input",
                }
            )

        # Current datetime as ISO string. ``_now`` is a test/caller override
        # (matches the ``_``-prefixed injected-key convention); otherwise use the
        # naive local wall-clock, as the source library expects.
        current_date_str = arguments.get("_now") or datetime.now().isoformat()

        try:
            result = compute_schedule(arguments, current_date_str)
        except Exception as e:
            # The date library can raise (e.g. Jan 31 + 1 month) and bad
            # recurrence/constraint fields raise ValueError; surface as a
            # structured observation, never a crash.
            logger.warning(f"[scheduler] conversion failed: {e}")
            return BuiltInToolResult(
                structured_content={
                    "error": "ConversionError",
                    "parsable": False,
                    "message": f"Failed to compute schedule: {e}",
                }
            )

        logger.info(f"[scheduler] Converted result: {result}")
        return BuiltInToolResult(structured_content=result)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server.

    Always includes the ``scheduler`` parser. The Calendar & Schedule action
    subtools (create/list/update/cancel schedule events) are appended here and
    gated per-request by :func:`get_prepare_tools` against the profile's
    ``calendar_schedule_enabled`` flag, so when the feature is OFF the agent
    only ever sees the parser (today's behavior).
    """
    tools: list[BuiltInTool] = [SchedulerTool()]
    try:
        from app.tools.builtin.scheduler_actions import get_action_tools
        tools.extend(get_action_tools(config))
    except Exception as exc:  # noqa: BLE001
        # Never let the action subtools break the always-on parser.
        logger.warning(f"[scheduler] action subtools unavailable: {exc}")
    return tools


def get_prepare_tools():
    """Per-request gate: hide the Calendar & Schedule action subtools unless the
    active profile has the feature enabled.

    The Reasoning Agent passes the active ``profile`` into every ``prepare_tools``
    callback (same plumbing ``change_working_directory`` relies on). We read the
    per-profile ``calendar_schedule_enabled`` flag; when OFF (the default), every
    action subtool is stripped from the tool list so the LLM only ever sees the
    always-on ``scheduler`` parser — i.e. today's behavior.

    Boot-safe: if the action-subtool module is not importable yet, there is
    nothing to gate and the tool list passes through unchanged.
    """

    def prepare_tools(query, tools, *, arguments=None, context_id=None, profile=None, **_):
        try:
            from app.tools.builtin.scheduler_actions import ACTION_TOOL_NAMES
        except Exception:  # noqa: BLE001
            return tools
        enabled = False
        if profile:
            try:
                from app.calendar.feature import is_enabled
                enabled = is_enabled(profile)
            except Exception:  # noqa: BLE001
                enabled = False
        if enabled:
            return tools
        return [
            t for t in tools
            if (t.get("function") or {}).get("name") not in ACTION_TOOL_NAMES
        ]

    return prepare_tools
