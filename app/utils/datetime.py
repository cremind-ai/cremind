"""Datetime decomposition + computation library.

Ported from the ``a2a-datetime-parser-agent`` project. The contract:

* An LLM decomposes a natural-language time expression into *atomic offsets*
  (relative/absolute year/month/day/hour/minute/second + weekday occurrences)
  rather than computing dates itself — the model must never rely on its own
  notion of "now".
* This library takes those offsets plus a concrete ``current_date_str`` and
  computes the actual datetime(s) in pure Python.

The public entry point is :func:`convert_datetime_payload`. The
``datetime_parser`` built-in tool (:mod:`app.tools.builtin.datetime_parser`)
builds a :class:`TimeInputPayload` from the LLM's tool-call arguments and calls
it.

All datetimes are naive local wall-clock (no timezone offset) and serialized as
``%Y-%m-%dT%H:%M:%S`` — consumers (calendar, reminders) own localization.
"""

from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, model_validator


class AbsoluteTime(BaseModel):
    """Represents absolute time values."""
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    second: Optional[int] = None


class RelativeTime(BaseModel):
    """Represents relative time offsets."""
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    second: Optional[int] = None


class WeekdayOffset(BaseModel):
    """Represents a weekday with an occurrence offset.

    E.g. WeekdayOffset(name='monday', offset=1) => next Monday
         WeekdayOffset(name='friday', offset=-1) => last Friday
         WeekdayOffset(name='wednesday', offset=0) => this Wednesday
    """
    name: str
    offset: int


class TimeSingle(BaseModel):
    """Represents a single point in time."""
    absolute: Optional[AbsoluteTime] = None
    relative: Optional[RelativeTime] = None
    weekday: Optional[WeekdayOffset] = None
    now: Optional[bool] = None


class TimeRangeDate(BaseModel):
    """Represents a date in a time range."""
    absolute: Optional[AbsoluteTime] = None
    relative: Optional[RelativeTime] = None
    weekday: Optional[WeekdayOffset] = None
    now: Optional[bool] = None


class TimeRange(BaseModel):
    """Represents a time range with start and end dates."""
    start_date: TimeRangeDate
    end_date: TimeRangeDate


class ComputedDateTime(BaseModel):
    """Represents a computed date/time result.

    Note: callers serialize with ``model_dump(exclude_none=True)`` so that an
    unset ``now``/``datetime`` is dropped from the output rather than rendered
    as ``null``.
    """
    now: Optional[bool] = None
    datetime: Optional[str] = None


class TimeInputPayload(BaseModel):
    """Input payload for time conversion.

    Note: Only one of time_single or time_range can be set at a time.
    """
    time_single: Optional[TimeSingle] = None
    time_range: Optional[TimeRange] = None

    @model_validator(mode='after')
    def validate_exclusive_fields(self):
        """Ensure only one of time_single or time_range is set."""
        if self.time_single is not None and self.time_range is not None:
            raise ValueError(
                "Only one of 'time_single' or 'time_range' can be set at a time, not both."
            )
        return self


class TimeConvertedPayload(BaseModel):
    """Output payload after time conversion.

    Note: Only one of time_single or time_range will be set.
    """
    parsable: bool
    reason: Optional[str] = None
    time_single: Optional[ComputedDateTime] = None
    time_range: Optional[dict[str, ComputedDateTime]] = None


# Weekday name to Python weekday index (Monday=0, Sunday=6)
WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Date-level units (vs time-of-day units). Used when deciding whether an
# endpoint already carries its own date or must inherit one from its sibling.
DATE_UNITS = {"year", "month", "day"}


def build_components(
    elements: list[dict],
) -> tuple[Optional[AbsoluteTime], Optional[RelativeTime], Optional[WeekdayOffset]]:
    """Build the absolute/relative/weekday components from a list of atomic
    time elements (the ``time_elements`` items emitted by the parsing LLM).

    Each element is a dict ``{mode, time_range, offset_unit, offset_value}``.
    ``mode`` is ``"absolute"`` or ``"relative"``; ``offset_unit`` is a
    year/month/day/hour/minute/second field or a weekday name. For weekday
    units the ``mode`` is ignored and a :class:`WeekdayOffset` is produced.

    Returns ``(abs_time | None, rel_time | None, weekday_offset | None)`` where
    ``abs_time``/``rel_time`` are ``None`` when no element of that mode was seen.
    """
    abs_time = AbsoluteTime()
    rel_time = RelativeTime()
    weekday_offset = None
    has_abs = False
    has_rel = False

    for elem in elements:
        unit = elem.get("offset_unit", "")
        value = elem.get("offset_value", 0)
        mode = elem.get("mode")  # ignored for weekday elements

        # Weekday element
        if unit in WEEKDAY_MAP:
            weekday_offset = WeekdayOffset(name=unit, offset=value)
            continue

        # Regular time element
        if mode == "absolute":
            has_abs = True
            if hasattr(abs_time, unit):
                setattr(abs_time, unit, value)
        elif mode == "relative":
            has_rel = True
            if hasattr(rel_time, unit):
                setattr(rel_time, unit, value)

    return (
        abs_time if has_abs else None,
        rel_time if has_rel else None,
        weekday_offset,
    )


def build_payload_from_elements(time_elements: list[dict]) -> TimeInputPayload:
    """Convert a flat list of atomic time elements into a :class:`TimeInputPayload`.

    Partitions the elements into start/end groups by their ``time_range`` tag,
    applies date inheritance (if the end group lacks date-level units it copies
    them from the start group, so "next Friday 10am-12pm" gives both endpoints
    Friday's date), and builds either a :class:`TimeRange` (when end elements
    exist) or a :class:`TimeSingle`.

    This does NOT decide ``single_time_mode`` and does NOT call
    :func:`convert_datetime_payload` — callers own those steps.
    """
    start_elements = []
    end_elements = []

    for elem in time_elements:
        tr = elem.get("time_range", "start")
        if tr == "end":
            end_elements.append(elem)
        else:
            start_elements.append(elem)

    # ── Date inheritance: if end group lacks date-level units, copy from start ──
    def _has_date_units(elements):
        for e in elements:
            unit = e.get("offset_unit", "")
            if unit in DATE_UNITS or unit in WEEKDAY_MAP:
                return True
        return False

    if end_elements and not _has_date_units(end_elements):
        for e in start_elements:
            unit = e.get("offset_unit", "")
            if unit in DATE_UNITS or unit in WEEKDAY_MAP:
                inherited = dict(e)
                inherited["time_range"] = "end"
                end_elements.append(inherited)

    # ── Build payload ──
    if end_elements:
        start_abs, start_rel, start_wd = build_components(start_elements)
        end_abs, end_rel, end_wd = build_components(end_elements)
        return TimeInputPayload(
            time_range=TimeRange(
                start_date=TimeRangeDate(
                    absolute=start_abs,
                    relative=start_rel,
                    weekday=start_wd,
                ),
                end_date=TimeRangeDate(
                    absolute=end_abs,
                    relative=end_rel,
                    weekday=end_wd,
                ),
            )
        )
    elif start_elements:
        start_abs, start_rel, start_wd = build_components(start_elements)
        return TimeInputPayload(
            time_single=TimeSingle(
                absolute=start_abs,
                relative=start_rel,
                weekday=start_wd,
            )
        )
    return TimeInputPayload()


def compute_weekday_date(current: datetime, weekday_name: str, offset: int) -> datetime:
    """Compute a target date based on a weekday name and occurrence offset.

    Args:
        current: The current datetime
        weekday_name: Lowercase weekday name (e.g., 'monday')
        offset: Occurrence offset:
            offset=1  => next occurrence of that weekday
            offset=2  => the occurrence after next
            offset=-1 => last (most recent past) occurrence
            offset=-2 => the occurrence before last
            offset=0  => this week's occurrence (could be past or future within the week)

    Returns:
        A datetime with the target date (time portion preserved from current)
    """
    target_wd = WEEKDAY_MAP.get(weekday_name.lower())
    if target_wd is None:
        return current  # Unknown weekday, return unchanged

    current_wd = current.weekday()  # Monday=0 .. Sunday=6

    if offset > 0:
        # Future occurrences
        days_ahead = (target_wd - current_wd) % 7
        if days_ahead == 0:
            days_ahead = 7  # If today is the target day, go to next week
        days_ahead += (offset - 1) * 7
        return current + timedelta(days=days_ahead)
    elif offset < 0:
        # Past occurrences
        days_back = (current_wd - target_wd) % 7
        if days_back == 0:
            days_back = 7  # If today is the target day, go to last week
        days_back += (abs(offset) - 1) * 7
        return current - timedelta(days=days_back)
    else:
        # offset == 0: this week's occurrence
        days_delta = target_wd - current_wd
        return current + timedelta(days=days_delta)


def convert_datetime_payload(payload: TimeInputPayload, current_date_str: str, single_time_mode: bool = True) -> TimeConvertedPayload:
    """
    Convert datetime payload to computed datetime results.

    Args:
        payload: The input time payload
        current_date_str: The current datetime as a string (ISO format)
        single_time_mode: If True, return a single computed datetime for time_single inputs.
                          If False, convert time_single inputs into a time_range based on the finest time unit.

    Returns:
        TimeConvertedPayload with converted time information
    """
    current = datetime.fromisoformat(current_date_str.replace('Z', '+00:00'))

    def is_empty_time_object(time_obj: Optional[TimeSingle | TimeRangeDate]) -> bool:
        """Check if a time object is None or empty."""
        if time_obj is None:
            return True
        if time_obj.now:
            return False
        if time_obj.weekday:
            return False
        if time_obj.absolute:
            abs_time = time_obj.absolute
            if any([abs_time.year is not None, abs_time.month is not None, abs_time.day is not None,
                    abs_time.hour is not None, abs_time.minute is not None, abs_time.second is not None]):
                return False
        if time_obj.relative:
            rel_time = time_obj.relative
            if any([rel_time.year is not None, rel_time.month is not None, rel_time.day is not None,
                    rel_time.hour is not None, rel_time.minute is not None, rel_time.second is not None]):
                return False
        return True

    def has_time_units(t: Optional[TimeSingle | TimeRangeDate]) -> bool:
        """Check if a time object has explicit time units (hour/minute/second)."""
        if not t:
            return False
        abs_time = t.absolute or AbsoluteTime()
        rel_time = t.relative or RelativeTime()
        return (abs_time.hour is not None or abs_time.minute is not None or abs_time.second is not None or
                rel_time.hour is not None or rel_time.minute is not None or rel_time.second is not None)

    def build_expanded_endpoint(t_obj: Optional[TimeRangeDate], is_start: bool) -> ComputedDateTime:
        """Build an expanded endpoint for a time range when no explicit time units."""
        # Check for 'now' flag first
        if t_obj and t_obj.now:
            return ComputedDateTime(now=True)

        base_start = datetime(current.year, current.month, current.day,
                              current.hour, current.minute, current.second)
        base_end = datetime(current.year, current.month, current.day,
                            current.hour, current.minute, current.second)

        if not t_obj:
            # No object provided -> return full-day for current day
            base_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0)
            base_end = base_end.replace(hour=23, minute=59, second=59, microsecond=999000)
            chosen = base_start if is_start else base_end
            return ComputedDateTime(
                datetime=chosen.strftime('%Y-%m-%dT%H:%M:%S')
            )

        # Apply weekday offset first (sets date portion)
        has_weekday = t_obj.weekday is not None
        if has_weekday:
            wd_date = compute_weekday_date(base_start, t_obj.weekday.name, t_obj.weekday.offset)
            base_start = base_start.replace(year=wd_date.year, month=wd_date.month, day=wd_date.day)
            base_end = base_end.replace(year=wd_date.year, month=wd_date.month, day=wd_date.day)

        # Apply relative shifts
        if t_obj.relative:
            rel = t_obj.relative
            if rel.year is not None:
                base_start = base_start.replace(year=base_start.year + rel.year)
                base_end = base_end.replace(year=base_end.year + rel.year)
            if rel.month is not None:
                # Handle month overflow
                new_month_start = base_start.month + rel.month
                new_month_end = base_end.month + rel.month
                year_offset_start = (new_month_start - 1) // 12
                year_offset_end = (new_month_end - 1) // 12
                base_start = base_start.replace(
                    year=base_start.year + year_offset_start,
                    month=((new_month_start - 1) % 12) + 1
                )
                base_end = base_end.replace(
                    year=base_end.year + year_offset_end,
                    month=((new_month_end - 1) % 12) + 1
                )
            if rel.day is not None:
                base_start += timedelta(days=rel.day)
                base_end += timedelta(days=rel.day)
            if rel.hour is not None:
                base_start += timedelta(hours=rel.hour)
                base_end += timedelta(hours=rel.hour)
            if rel.minute is not None:
                base_start += timedelta(minutes=rel.minute)
                base_end += timedelta(minutes=rel.minute)
            if rel.second is not None:
                base_start += timedelta(seconds=rel.second)
                base_end += timedelta(seconds=rel.second)

        # Then apply absolute overrides (if present)
        if t_obj.absolute:
            abs_time = t_obj.absolute
            if abs_time.year is not None:
                base_start = base_start.replace(year=abs_time.year)
                base_end = base_end.replace(year=abs_time.year)
            if abs_time.month is not None:
                base_start = base_start.replace(month=abs_time.month)
                base_end = base_end.replace(month=abs_time.month)
            if abs_time.day is not None:
                base_start = base_start.replace(day=abs_time.day)
                base_end = base_end.replace(day=abs_time.day)
            if abs_time.hour is not None or abs_time.minute is not None:
                h = abs_time.hour if abs_time.hour is not None else base_start.hour
                m = abs_time.minute if abs_time.minute is not None else (
                    0 if abs_time.hour is not None else base_start.minute)
                s = abs_time.second if abs_time.second is not None else 0
                base_start = base_start.replace(hour=h, minute=m, second=s, microsecond=0)
                base_end = base_end.replace(hour=h, minute=m, second=s, microsecond=0)
            elif abs_time.second is not None:
                base_start = base_start.replace(second=abs_time.second, microsecond=0)
                base_end = base_end.replace(second=abs_time.second, microsecond=0)

        # Determine if the input mentioned day/month/year/weekday
        abs_time = t_obj.absolute or AbsoluteTime()
        rel_time = t_obj.relative or RelativeTime()
        has_day = abs_time.day is not None or rel_time.day is not None or has_weekday
        has_month = abs_time.month is not None or rel_time.month is not None
        has_year = abs_time.year is not None or rel_time.year is not None

        # Special case: if only relative.day=0 (no other fields), treat as current moment
        if (has_day and not has_month and not has_year and
            rel_time.day == 0 and abs_time.day is None and
            rel_time.month is None and rel_time.year is None and
                abs_time.month is None and abs_time.year is None):
            # Return current moment, not full day range
            chosen = base_start if is_start else base_end
            return ComputedDateTime(datetime=chosen.strftime('%Y-%m-%dT%H:%M:%S'))

        # Build range when no explicit time units
        if has_day:
            base_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0)
            base_end = base_end.replace(hour=23, minute=59, second=59, microsecond=999000)
        elif has_month:
            # Start = first day of month 00:00:00
            base_start = base_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            # End = last day of month 23:59:59
            if base_end.month == 12:
                last_day = datetime(base_end.year + 1, 1, 1) - timedelta(days=1)
            else:
                last_day = datetime(base_end.year, base_end.month + 1, 1) - timedelta(days=1)
            base_end = base_end.replace(day=last_day.day, hour=23, minute=59, second=59, microsecond=999000)
        elif has_year:
            # Start = Jan 1 00:00:00
            base_start = base_start.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

            # End = Dec 31 23:59:59
            base_end = base_end.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=999000)
        else:
            # No day/month/year specified: treat as full current day
            base_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0)
            base_end = base_end.replace(hour=23, minute=59, second=59, microsecond=999000)

        # Choose which endpoint to return
        chosen = base_start if is_start else base_end
        return ComputedDateTime(datetime=chosen.strftime('%Y-%m-%dT%H:%M:%S'))

    # Check if the payload is parsable
    parsable = True
    reason = None

    # Handle case when neither time_single nor time_range is provided
    if not payload.time_single and not payload.time_range:
        parsable = False
        reason = "No datetime information provided in the input"
        return TimeConvertedPayload(
            parsable=parsable,
            reason=reason
        )

    # Handle time_single
    if payload.time_single:
        if is_empty_time_object(payload.time_single):
            parsable = False
            reason = "time_single is empty or contains no datetime data"
            return TimeConvertedPayload(
                parsable=parsable,
                reason=reason
            )

        if single_time_mode:
            time_single_result = compute_date_time(payload.time_single, current)
            return TimeConvertedPayload(
                parsable=parsable,
                reason=reason,
                time_single=time_single_result
            )
        else:
            # Convert single time to a range based on the finest time unit referenced
            start_computed, end_computed = compute_single_to_range(payload.time_single, current)
            return TimeConvertedPayload(
                parsable=parsable,
                reason=reason,
                time_range={
                    'start_date': start_computed,
                    'end_date': end_computed
                }
            )

    elif payload.time_range:
        # Handle time_range
        tr = payload.time_range

        # Check if both start and end are empty
        start_empty = is_empty_time_object(tr.start_date)
        end_empty = is_empty_time_object(tr.end_date)

        if start_empty and end_empty:
            parsable = False
            reason = "Both start_date and end_date are empty or contain no datetime data"

        # Process start and end endpoints
        start_has_time = has_time_units(tr.start_date)
        end_has_time = has_time_units(tr.end_date)

        if start_has_time:
            start_computed = compute_date_time(tr.start_date, current)
        else:
            start_computed = build_expanded_endpoint(tr.start_date, True)

        if end_has_time:
            end_computed = compute_date_time(tr.end_date, current)
        else:
            end_computed = build_expanded_endpoint(tr.end_date, False)

        return TimeConvertedPayload(
            parsable=parsable,
            reason=reason,
            time_range={
                'start_date': start_computed,
                'end_date': end_computed
            }
        )

    # Fallback case (should not be reached)
    return TimeConvertedPayload(
        parsable=False,
        reason="Invalid payload structure"
    )


def compute_date_time(time_obj: TimeSingle | TimeRangeDate, current: datetime) -> ComputedDateTime:
    """
    Compute a single date/time from a time object.

    Args:
        time_obj: The time specification object
        current: The current datetime

    Returns:
        ComputedDateTime with the computed date and optionally time
    """
    if time_obj.now:
        return ComputedDateTime(now=True)

    dt = datetime(current.year, current.month, current.day,
                  current.hour, current.minute, current.second)

    # Apply weekday offset first (sets date portion)
    if time_obj.weekday:
        wd_date = compute_weekday_date(dt, time_obj.weekday.name, time_obj.weekday.offset)
        dt = dt.replace(year=wd_date.year, month=wd_date.month, day=wd_date.day)

    # Apply relative shifts
    if time_obj.relative:
        rel = time_obj.relative
        if rel.year is not None:
            dt = dt.replace(year=dt.year + rel.year)
        if rel.month is not None:
            new_month = dt.month + rel.month
            year_offset = (new_month - 1) // 12
            dt = dt.replace(
                year=dt.year + year_offset,
                month=((new_month - 1) % 12) + 1
            )
        if rel.day is not None:
            dt += timedelta(days=rel.day)
        if rel.hour is not None:
            dt += timedelta(hours=rel.hour)
        if rel.minute is not None:
            dt += timedelta(minutes=rel.minute)
        if rel.second is not None:
            dt += timedelta(seconds=rel.second)

    # Apply absolute overrides
    if time_obj.absolute:
        abs_time = time_obj.absolute
        if abs_time.year is not None:
            dt = dt.replace(year=abs_time.year)
        if abs_time.month is not None:
            dt = dt.replace(month=abs_time.month)
        if abs_time.day is not None:
            dt = dt.replace(day=abs_time.day)

        if abs_time.hour is not None or abs_time.minute is not None:
            h = abs_time.hour if abs_time.hour is not None else dt.hour
            m = abs_time.minute if abs_time.minute is not None else (0 if abs_time.hour is not None else dt.minute)
            s = abs_time.second if abs_time.second is not None else 0
            dt = dt.replace(hour=h, minute=m, second=s, microsecond=0)
        elif abs_time.second is not None:
            dt = dt.replace(second=abs_time.second, microsecond=0)

    # Always return datetime in ISO format
    return ComputedDateTime(datetime=dt.strftime('%Y-%m-%dT%H:%M:%S'))


def compute_single_to_range(
    time_obj: TimeSingle | TimeRangeDate, current: datetime
) -> tuple[ComputedDateTime, ComputedDateTime]:
    """Convert a single time object into a (start, end) range.

    The range is determined by the finest time unit referenced:
      - second  → that exact second
      - minute  → XX:YY:00  …  XX:YY:59
      - hour    → XX:00:00  …  XX:59:59
      - day / weekday → 00:00:00  …  23:59:59
      - month   → 1st 00:00:00  …  last-day 23:59:59
      - year    → Jan-1 00:00:00  …  Dec-31 23:59:59
      - (none)  → full current day

    Args:
        time_obj: The time specification (TimeSingle or TimeRangeDate).
        current:  The reference "now" datetime.

    Returns:
        A tuple of (start ComputedDateTime, end ComputedDateTime).
    """
    if time_obj.now:
        now_result = ComputedDateTime(now=True)
        return now_result, now_result

    base_start = datetime(
        current.year, current.month, current.day,
        current.hour, current.minute, current.second,
    )
    base_end = datetime(
        current.year, current.month, current.day,
        current.hour, current.minute, current.second,
    )

    # ── 1. Weekday offset ──────────────────────────────────────────────
    has_weekday = time_obj.weekday is not None
    if has_weekday:
        wd_date = compute_weekday_date(base_start, time_obj.weekday.name, time_obj.weekday.offset)
        base_start = base_start.replace(year=wd_date.year, month=wd_date.month, day=wd_date.day)
        base_end = base_end.replace(year=wd_date.year, month=wd_date.month, day=wd_date.day)

    # ── 2. Relative shifts ─────────────────────────────────────────────
    if time_obj.relative:
        rel = time_obj.relative
        if rel.year is not None:
            base_start = base_start.replace(year=base_start.year + rel.year)
            base_end = base_end.replace(year=base_end.year + rel.year)
        if rel.month is not None:
            new_month_s = base_start.month + rel.month
            new_month_e = base_end.month + rel.month
            y_off_s = (new_month_s - 1) // 12
            y_off_e = (new_month_e - 1) // 12
            base_start = base_start.replace(
                year=base_start.year + y_off_s,
                month=((new_month_s - 1) % 12) + 1,
            )
            base_end = base_end.replace(
                year=base_end.year + y_off_e,
                month=((new_month_e - 1) % 12) + 1,
            )
        if rel.day is not None:
            base_start += timedelta(days=rel.day)
            base_end += timedelta(days=rel.day)
        if rel.hour is not None:
            base_start += timedelta(hours=rel.hour)
            base_end += timedelta(hours=rel.hour)
        if rel.minute is not None:
            base_start += timedelta(minutes=rel.minute)
            base_end += timedelta(minutes=rel.minute)
        if rel.second is not None:
            base_start += timedelta(seconds=rel.second)
            base_end += timedelta(seconds=rel.second)

    # ── 3. Absolute overrides ──────────────────────────────────────────
    if time_obj.absolute:
        a = time_obj.absolute
        if a.year is not None:
            base_start = base_start.replace(year=a.year)
            base_end = base_end.replace(year=a.year)
        if a.month is not None:
            base_start = base_start.replace(month=a.month)
            base_end = base_end.replace(month=a.month)
        if a.day is not None:
            base_start = base_start.replace(day=a.day)
            base_end = base_end.replace(day=a.day)
        if a.hour is not None or a.minute is not None:
            h = a.hour if a.hour is not None else base_start.hour
            m = a.minute if a.minute is not None else (
                0 if a.hour is not None else base_start.minute
            )
            s = a.second if a.second is not None else 0
            base_start = base_start.replace(hour=h, minute=m, second=s, microsecond=0)
            base_end = base_end.replace(hour=h, minute=m, second=s, microsecond=0)
        elif a.second is not None:
            base_start = base_start.replace(second=a.second, microsecond=0)
            base_end = base_end.replace(second=a.second, microsecond=0)

    # ── 4. Determine finest referenced unit and expand range ───────────
    abs_t = time_obj.absolute or AbsoluteTime()
    rel_t = time_obj.relative or RelativeTime()

    has_second = abs_t.second is not None or rel_t.second is not None
    has_minute = abs_t.minute is not None or rel_t.minute is not None
    has_hour   = abs_t.hour   is not None or rel_t.hour   is not None
    has_day    = abs_t.day    is not None or rel_t.day    is not None or has_weekday
    has_month  = abs_t.month  is not None or rel_t.month  is not None
    has_year   = abs_t.year   is not None or rel_t.year   is not None

    if has_second:
        # Exact second – no expansion needed
        pass
    elif has_minute:
        base_start = base_start.replace(second=0, microsecond=0)
        base_end   = base_end.replace(second=59, microsecond=0)
    elif has_hour:
        base_start = base_start.replace(minute=0, second=0, microsecond=0)
        base_end   = base_end.replace(minute=59, second=59, microsecond=0)
    elif has_day:
        base_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0)
        base_end   = base_end.replace(hour=23, minute=59, second=59, microsecond=0)
    elif has_month:
        base_start = base_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if base_end.month == 12:
            last_day = datetime(base_end.year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = datetime(base_end.year, base_end.month + 1, 1) - timedelta(days=1)
        base_end = base_end.replace(
            day=last_day.day, hour=23, minute=59, second=59, microsecond=0,
        )
    elif has_year:
        base_start = base_start.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        base_end   = base_end.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0)
    else:
        # No explicit unit → treat as current day
        base_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0)
        base_end   = base_end.replace(hour=23, minute=59, second=59, microsecond=0)

    return (
        ComputedDateTime(datetime=base_start.strftime('%Y-%m-%dT%H:%M:%S')),
        ComputedDateTime(datetime=base_end.strftime('%Y-%m-%dT%H:%M:%S')),
    )
