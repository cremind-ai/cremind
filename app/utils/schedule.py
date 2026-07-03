"""Schedule normalization + computation library.

The companion of :mod:`app.utils.datetime`. Where that module turns a single
natural-language *time* expression into concrete datetimes, this module turns a
*schedule* expression — already decomposed by the parsing LLM into the
``scheduler`` tool-call schema — into a normalized, machine-usable result
for downstream calendar/reminder services (Google Calendar, iCloud/CalDAV,
Reminders).

A schedule is classified into one of six core data types:

* **instant**      — a single point ("at 9 AM")
* **interval**     — a bounded span, start+end or start+duration ("2-4 PM")
* **recurrence**   — a generator rule ("every weekday at 9")
* **explicit_set** — a hand-listed union ("July 3 and July 5 at 2pm")
* **window**       — a query region, not a booking ("this week")
* **constraint**   — a predicate that qualifies any of the above ("weekday
  afternoons only")

The time-of-day / date anchors of every kind are decomposed with the SAME
atomic-offset elements as ``datetime_parser`` and computed by reusing
:func:`app.utils.datetime.convert_datetime_payload` (via
:func:`app.utils.datetime.build_payload_from_elements`) — this module never
invents new datetime math; it only composes the existing library and builds an
RFC 5545 RRULE string for recurrences.

All datetimes are naive local wall-clock (``%Y-%m-%dT%H:%M:%S``). The result
carries a ``timezone: "pending"`` marker; the consuming tool owns localization
(notably: an RRULE ``UNTIL`` for a timed series must be UTC-``Z``, so the end
bound is emitted separately in ``recurrence_end`` rather than baked into the
naive RRULE string).
"""

from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel

from app.utils.datetime import (
    build_payload_from_elements,
    convert_datetime_payload,
)


# Default span (minutes) applied when a point-in-time needs an end for a
# calendar that requires start+end (e.g. an instant booked as a 30-min event).
DEFAULT_DURATION_MINUTES = 30

# Per-kind downstream intent: book = create event(s); query = list/freebusy;
# filter = qualify a candidate set.
_INTENT_BY_KIND = {
    "instant": "book",
    "interval": "book",
    "recurrence": "book",
    "explicit_set": "book",
    "window": "query",
    "constraint": "filter",
}

# RRULE FREQ mapping.
_FREQ_MAP = {
    "secondly": "SECONDLY",
    "minutely": "MINUTELY",
    "hourly": "HOURLY",
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
    "yearly": "YEARLY",
}

# Weekday two-letter codes in Monday=0 .. Sunday=6 order (matches WEEKDAY_MAP).
_WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_CODE_TO_INDEX = {code: i for i, code in enumerate(_WEEKDAY_CODES)}
_NAME_TO_CODE = {
    "monday": "MO",
    "tuesday": "TU",
    "wednesday": "WE",
    "thursday": "TH",
    "friday": "FR",
    "saturday": "SA",
    "sunday": "SU",
}

# Named time-of-day bands -> (start_hour, end_hour). end_hour 24 means midnight.
_NAMED_BANDS = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
    "night": (22, 24),
    "business_hours": (9, 17),
}


# ── Schedule-specific models ───────────────────────────────────────────────


class Duration(BaseModel):
    """A length of time for a start+duration interval ("for 2 hours").

    Months/years are intentionally absent — those are ambiguous as lengths and
    are expressed via end ``time_elements`` instead (which also avoids the
    Jan-31 + 1-month overflow in the date library).
    """

    days: Optional[int] = None
    hours: Optional[int] = None
    minutes: Optional[int] = None
    seconds: Optional[int] = None

    def to_timedelta(self) -> timedelta:
        return timedelta(
            days=self.days or 0,
            hours=self.hours or 0,
            minutes=self.minutes or 0,
            seconds=self.seconds or 0,
        )

    def is_empty(self) -> bool:
        return not any([self.days, self.hours, self.minutes, self.seconds])


class Recurrence(BaseModel):
    """RFC 5545-aligned recurrence rule fields."""

    frequency: str
    interval: Optional[int] = None
    by_weekday: Optional[list[str]] = None
    by_monthday: Optional[list[int]] = None
    by_month: Optional[list[int]] = None
    by_setpos: Optional[list[int]] = None
    by_hour: Optional[list[int]] = None
    by_minute: Optional[list[int]] = None
    count: Optional[int] = None


class ConstraintPredicate(BaseModel):
    """A single predicate that filters/qualifies a schedule."""

    type: str
    weekdays: Optional[list[str]] = None
    band: Optional[str] = None
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None
    range_start_elements: Optional[list[dict]] = None
    range_end_elements: Optional[list[dict]] = None
    excluded_elements: Optional[list[dict]] = None


class ScheduleMember(BaseModel):
    """One hand-listed member of an explicit set."""

    member_kind: str = "instant"
    time_elements: Optional[list[dict]] = None
    duration: Optional[Duration] = None


# ── Small datetime string helpers ──────────────────────────────────────────


def _iso_to_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")


def _dt_to_iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _not_parsable(detail: str) -> dict:
    return {"parsable": False, "reason": f"Could not compute {detail}"}


def _normalize_weekday_code(value: Any) -> str:
    """Accept either a two-letter code ('MO') or a full name ('monday')."""
    v = str(value).strip()
    if v.upper() in _CODE_TO_INDEX:
        return v.upper()
    if v.lower() in _NAME_TO_CODE:
        return _NAME_TO_CODE[v.lower()]
    raise ValueError(f"Unknown weekday: {value!r}")


# ── Reused datetime computation, wrapped per anchor ────────────────────────


def _compute_one_instant(time_elements: list[dict], now_str: str):
    """Compute a single :class:`ComputedDateTime` from atomic elements, or None."""
    payload = build_payload_from_elements(time_elements or [])
    res = convert_datetime_payload(payload, now_str, single_time_mode=True)
    if not res.parsable:
        return None
    return res.time_single


def _compute_range(time_elements: list[dict], now_str: str, single_time_mode: bool):
    """Compute a (start, end) pair of :class:`ComputedDateTime`, or None."""
    payload = build_payload_from_elements(time_elements or [])
    res = convert_datetime_payload(payload, now_str, single_time_mode=single_time_mode)
    if not res.parsable:
        return None
    if res.time_range:
        return res.time_range["start_date"], res.time_range["end_date"]
    if res.time_single:
        return res.time_single, res.time_single
    return None


# ── RRULE string builder (pure Python, RFC 5545) ───────────────────────────


def build_rrule(rec: Recurrence) -> str:
    """Build an RFC 5545 RRULE value (without the ``RRULE:`` prefix).

    Canonical part order: FREQ, INTERVAL, BYMONTH, BYMONTHDAY, BYDAY, BYHOUR,
    BYMINUTE, BYSETPOS, COUNT. UNTIL is deliberately NOT included — it requires
    a UTC-``Z`` value for timed series and this library emits naive local time,
    so the end bound is surfaced separately via ``recurrence_end``.

    When a single ``by_setpos`` accompanies ``by_weekday`` it is folded into a
    positional BYDAY (e.g. by_setpos=[1], by_weekday=['MO'] -> ``BYDAY=1MO``),
    the form calendars handle most reliably for "first/last X of the month".
    """
    freq = _FREQ_MAP.get(str(rec.frequency).lower())
    if freq is None:
        raise ValueError(f"Unsupported frequency: {rec.frequency!r}")

    parts = [f"FREQ={freq}"]

    if rec.interval and rec.interval != 1:
        parts.append(f"INTERVAL={rec.interval}")

    if rec.by_month:
        parts.append("BYMONTH=" + ",".join(str(m) for m in rec.by_month))

    if rec.by_monthday:
        parts.append("BYMONTHDAY=" + ",".join(str(d) for d in rec.by_monthday))

    setpos_consumed = False
    if rec.by_weekday:
        codes = [_normalize_weekday_code(d) for d in rec.by_weekday]
        if rec.by_setpos and len(rec.by_setpos) == 1:
            pos = rec.by_setpos[0]
            parts.append("BYDAY=" + ",".join(f"{pos}{c}" for c in codes))
            setpos_consumed = True
        else:
            parts.append("BYDAY=" + ",".join(codes))

    if rec.by_hour:
        parts.append("BYHOUR=" + ",".join(str(h) for h in rec.by_hour))

    if rec.by_minute:
        parts.append("BYMINUTE=" + ",".join(str(m) for m in rec.by_minute))

    if rec.by_setpos and not setpos_consumed:
        parts.append("BYSETPOS=" + ",".join(str(p) for p in rec.by_setpos))

    if rec.count:
        parts.append(f"COUNT={rec.count}")

    return ";".join(parts)


def preview_occurrences(
    rec: Recurrence,
    dtstart_iso: Optional[str],
    until_iso: Optional[str],
    n: int = 5,
    hard_cap: int = 50,
) -> Optional[list[str]]:
    """Advisory preview of the next occurrences. DAILY/WEEKLY only.

    The RRULE remains the source of truth — downstream services do the real
    expansion (BYSETPOS, leap years, DST, …). This is a small, bounded
    convenience for the reasoning agent / user confirmation. Returns None for
    other frequencies or when there's nothing to expand.
    """
    if dtstart_iso is None:
        return None
    freq = str(rec.frequency).lower()
    if freq not in ("daily", "weekly"):
        return None

    start = _iso_to_dt(dtstart_iso)
    interval = rec.interval if (rec.interval and rec.interval > 0) else 1
    limit = min(n, rec.count or n, hard_cap)
    until_dt = _iso_to_dt(until_iso) if until_iso else None
    out: list[str] = []

    if freq == "daily":
        step = timedelta(days=interval)
        cur = start
        while len(out) < limit:
            if until_dt and cur > until_dt:
                break
            out.append(_dt_to_iso(cur))
            cur += step
        return out or None

    # weekly
    codes = rec.by_weekday or []
    if codes:
        indices = sorted({_CODE_TO_INDEX[_normalize_weekday_code(c)] for c in codes})
    else:
        indices = [start.weekday()]
    week_anchor = start - timedelta(days=start.weekday())  # Monday of start's week
    safety = 0
    while len(out) < limit and safety < hard_cap:
        for wd in indices:
            occ = (week_anchor + timedelta(days=wd)).replace(
                hour=start.hour, minute=start.minute, second=start.second
            )
            if occ < start:
                continue
            if until_dt and occ > until_dt:
                return out or None
            out.append(_dt_to_iso(occ))
            if len(out) >= limit:
                break
        week_anchor += timedelta(weeks=interval)
        safety += 1
    return out or None


# ── Constraint normalization ───────────────────────────────────────────────


def _band_time(hour: Optional[int]) -> Optional[str]:
    if hour is None:
        return None
    if hour >= 24:
        return "23:59:59"
    return f"{hour:02d}:00:00"


def _resolve_band(c: ConstraintPredicate) -> tuple[Optional[int], Optional[int]]:
    if c.band and c.band != "custom":
        return _NAMED_BANDS.get(c.band, (None, None))
    return (c.start_hour, c.end_hour)


def normalize_constraints(raw_list: list[dict], now_str: str) -> list[dict]:
    """Turn raw constraint dicts into structured, downstream-ready predicates.

    Resolves named time-of-day bands to explicit hour ranges and adds
    ``weekday_indices`` (Monday=0) so a downstream availability filter can test
    ``slot.weekday() in indices`` without re-mapping. Date-bearing predicates
    are computed via the same offset machinery as everything else.
    """
    out: list[dict] = []
    for c_raw in raw_list:
        c = ConstraintPredicate(**c_raw)
        ctype = c.type

        if ctype == "weekday_membership":
            codes = [_normalize_weekday_code(w) for w in (c.weekdays or [])]
            out.append(
                {
                    "type": "weekday_membership",
                    "weekdays": codes,
                    "weekday_indices": [_CODE_TO_INDEX[w] for w in codes],
                }
            )

        elif ctype == "time_of_day_band":
            start_h, end_h = _resolve_band(c)
            entry: dict[str, Any] = {"type": "time_of_day_band"}
            if c.band:
                entry["band"] = c.band
            start_t = _band_time(start_h)
            end_t = _band_time(end_h)
            if start_t is not None:
                entry["start_time"] = start_t
            if end_t is not None:
                entry["end_time"] = end_t
            out.append(entry)

        elif ctype == "date_range":
            rs = _compute_one_instant(c.range_start_elements or [], now_str)
            re_ = _compute_one_instant(c.range_end_elements or [], now_str)
            entry = {"type": "date_range"}
            if rs is not None and rs.datetime is not None:
                entry["range_start"] = rs.datetime
            if re_ is not None and re_.datetime is not None:
                entry["range_end"] = re_.datetime
            out.append(entry)

        elif ctype == "excluded_dates":
            entry = {"type": "excluded_dates"}
            if c.excluded_elements:
                ex = _compute_one_instant(c.excluded_elements, now_str)
                if ex is not None and ex.datetime is not None:
                    entry["dates"] = [ex.datetime]
            if c.weekdays:
                codes = [_normalize_weekday_code(w) for w in c.weekdays]
                entry["weekdays"] = codes
                entry["weekday_indices"] = [_CODE_TO_INDEX[w] for w in codes]
            out.append(entry)

        else:
            raise ValueError(f"Unknown constraint type: {ctype!r}")

    return out


# ── Per-kind computation ───────────────────────────────────────────────────


def _compute_instant(arguments: dict, now_str: str) -> dict:
    cdt = _compute_one_instant(arguments.get("time_elements") or [], now_str)
    if cdt is None:
        return _not_parsable("instant")
    return {
        "instant": cdt.model_dump(exclude_none=True),
        "default_duration_minutes": DEFAULT_DURATION_MINUTES,
    }


def _compute_interval(arguments: dict, now_str: str) -> dict:
    time_elements = arguments.get("time_elements") or []
    duration = Duration(**(arguments.get("duration") or {}))

    if not duration.is_empty():
        cdt = _compute_one_instant(time_elements, now_str)
        if cdt is None or cdt.datetime is None:
            return _not_parsable("interval start")
        start_dt = _iso_to_dt(cdt.datetime)
        end_dt = start_dt + duration.to_timedelta()
        start_iso, end_iso = _dt_to_iso(start_dt), _dt_to_iso(end_dt)
    else:
        rng = _compute_range(time_elements, now_str, single_time_mode=False)
        if rng is None or rng[0].datetime is None or rng[1].datetime is None:
            return _not_parsable("interval range")
        start_iso, end_iso = rng[0].datetime, rng[1].datetime

    minutes = int((_iso_to_dt(end_iso) - _iso_to_dt(start_iso)).total_seconds() // 60)
    return {
        "interval": {
            "start": start_iso,
            "end": end_iso,
            "duration_minutes": minutes,
        }
    }


def _compute_window(arguments: dict, now_str: str) -> dict:
    # Windows describe a region: default to expansion unless the LLM forces a point.
    stm = arguments.get("single_time_mode", False)
    rng = _compute_range(arguments.get("time_elements") or [], now_str, single_time_mode=stm)
    if rng is None or rng[0].datetime is None or rng[1].datetime is None:
        return _not_parsable("window")
    return {"window": {"range_start": rng[0].datetime, "range_end": rng[1].datetime}}


def _compute_recurrence(arguments: dict, now_str: str) -> dict:
    rec_raw = arguments.get("recurrence")
    if not rec_raw or not rec_raw.get("frequency"):
        return _not_parsable("recurrence (missing frequency)")
    rec = Recurrence(**rec_raw)
    rrule = build_rrule(rec)

    # DTSTART = the time-of-day anchor computed as a single instant (optional).
    dtstart_iso = None
    dt = _compute_one_instant(arguments.get("time_elements") or [], now_str)
    if dt is not None and dt.datetime is not None:
        dtstart_iso = dt.datetime

    # Termination: until (date) wins over count; else open-ended.
    until_iso = None
    if arguments.get("until_elements"):
        u = _compute_one_instant(arguments["until_elements"], now_str)
        if u is not None and u.datetime is not None:
            until_iso = u.datetime

    # Bounded preview (daily/weekly). When available, snap DTSTART to the first
    # actual occurrence so it matches the rule (e.g. 'every weekday at 9'
    # anchored on a Saturday starts on the following Monday).
    preview = preview_occurrences(rec, dtstart_iso, until_iso)
    if preview:
        dtstart_iso = preview[0]

    # A bare sub-daily recurrence ("every 2 hours") has no time-of-day anchor and
    # no bounded preview, leaving dtstart unset — anchor it to now (rounded to the
    # minute) so its grid is fresh (starting ~now) and the LLM never has to invent,
    # or forward a stale, dtstart. Daily+ recurrences keep their existing behavior
    # (preview snap, or LLM-supplied anchor). Parse tolerantly: now_str may carry
    # microseconds.
    if not dtstart_iso and str(rec.frequency).lower() in ("secondly", "minutely", "hourly"):
        anchor = datetime.fromisoformat(now_str).replace(second=0, microsecond=0, tzinfo=None)
        dtstart_iso = _dt_to_iso(anchor)

    if until_iso:
        recurrence_end = {"type": "until", "value": until_iso}
    elif rec.count:
        recurrence_end = {"type": "count", "value": rec.count}
    else:
        recurrence_end = {"type": "never"}

    rec_payload: dict[str, Any] = {}
    if dtstart_iso:
        rec_payload["dtstart"] = dtstart_iso
    rec_payload["rrule"] = rrule
    rec_payload["recurrence"] = [f"RRULE:{rrule}"]
    rec_payload["recurrence_end"] = recurrence_end
    rec_payload["duration_minutes"] = DEFAULT_DURATION_MINUTES
    if preview:
        rec_payload["preview"] = preview

    return {"recurrence": rec_payload}


def _compute_explicit_set(arguments: dict, now_str: str) -> dict:
    members_raw = arguments.get("members") or []
    shared_elements = arguments.get("time_elements") or []
    occurrences: list[dict] = []

    for m_raw in members_raw:
        m = ScheduleMember(**m_raw)
        # Merge the shared anchor (e.g. a time stated once) with the member's
        # own distinguishing units (e.g. its date).
        elems = list(shared_elements) + list(m.time_elements or [])
        dur = m.duration or Duration()

        if not dur.is_empty():
            cdt = _compute_one_instant(elems, now_str)
            if cdt is None or cdt.datetime is None:
                continue
            sdt = _iso_to_dt(cdt.datetime)
            edt = sdt + dur.to_timedelta()
            minutes = int((edt - sdt).total_seconds() // 60)
            occurrences.append(
                {"start": _dt_to_iso(sdt), "end": _dt_to_iso(edt), "duration_minutes": minutes}
            )
        elif m.member_kind == "interval":
            rng = _compute_range(elems, now_str, single_time_mode=False)
            if rng is None or rng[0].datetime is None or rng[1].datetime is None:
                continue
            occurrences.append({"start": rng[0].datetime, "end": rng[1].datetime})
        else:  # instant member
            cdt = _compute_one_instant(elems, now_str)
            if cdt is None or cdt.datetime is None:
                continue
            occurrences.append({"instant": cdt.datetime})

    if not occurrences:
        return _not_parsable("explicit_set (no parsable members)")
    return {"explicit_set": {"occurrences": occurrences}}


# ── Public entry point ─────────────────────────────────────────────────────


def compute_schedule(arguments: dict, now_str: str) -> dict:
    """Compute a normalized schedule result from the LLM's tool-call arguments.

    ``arguments`` is the ``scheduler`` tool-call payload; ``now_str`` is
    the current naive-local datetime as an ISO string. Returns a dict suitable
    for ``BuiltInToolResult.structured_content``. On any per-kind failure
    returns ``{"parsable": False, "reason": ...}`` (no envelope). Raising is the
    caller's signal to surface a structured ConversionError; this function does
    not catch — the tool's ``run`` wraps it.
    """
    kind = arguments.get("schedule_kind")
    constraints = normalize_constraints(arguments.get("constraints") or [], now_str)

    if kind == "instant":
        payload = _compute_instant(arguments, now_str)
    elif kind == "interval":
        payload = _compute_interval(arguments, now_str)
    elif kind == "window":
        payload = _compute_window(arguments, now_str)
    elif kind == "recurrence":
        payload = _compute_recurrence(arguments, now_str)
    elif kind == "explicit_set":
        payload = _compute_explicit_set(arguments, now_str)
    elif kind == "constraint":
        if not constraints:
            return {
                "parsable": False,
                "reason": arguments.get("reasoning") or "No constraint predicates provided",
            }
        payload = {}
    else:
        return {"parsable": False, "reason": f"Unknown schedule_kind: {kind!r}"}

    if payload.get("parsable") is False:
        return payload

    result: dict[str, Any] = {
        "parsable": True,
        "schedule_kind": kind,
        "intent": _INTENT_BY_KIND.get(kind, "book"),
        "timezone": "pending",
    }
    result.update(payload)
    result["constraints"] = constraints
    return result
