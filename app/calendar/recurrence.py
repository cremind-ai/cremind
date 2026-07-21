"""Recurrence math for the Calendar & Schedule engine — the rolling pointer.

The ``ScheduleManager`` never materializes a recurrence's occurrences. It keeps
ONE ``next_fire_at`` per rule and, after each fire, calls
:func:`next_occurrence_after` to advance it. The calendar UI calls
:func:`expand_occurrences` to render a recurrence within the visible window only.

All datetimes here are naive local wall-clock — the same convention the
``scheduler`` parser emits (``%Y-%m-%dT%H:%M:%S``). ``next_fire_at`` is stored as
epoch seconds; :func:`to_epoch` / :func:`from_epoch` bridge the two (a naive
datetime's ``.timestamp()`` is interpreted in the machine's local zone, which is
exactly what we want for a wall-clock schedule).

RFC 5545 expansion is delegated to ``python-dateutil`` (a core dependency). A
``COUNT`` bound lives inside the RRULE string (``build_rrule`` emits it), so
dateutil enforces it natively; a ``UNTIL`` date bound is kept out of the naive
string (it needs UTC ``Z`` for timed series) and applied here as a cutoff.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import List, Optional

from app.utils.logger import logger

_FMT = "%Y-%m-%dT%H:%M:%S"


# ── datetime <-> string / epoch bridges ─────────────────────────────────────

def parse_local(iso: str) -> datetime:
    """Parse a naive-local ISO string. Tolerates a missing seconds field."""
    s = (iso or "").strip()
    try:
        return datetime.strptime(s, _FMT)
    except ValueError:
        # Fall back to fromisoformat for variants ('...T09:00', date-only).
        return datetime.fromisoformat(s)


def format_local(dt: datetime) -> str:
    return dt.strftime(_FMT)


def _os_local_tz() -> tzinfo:
    """The process's OS-local zone — the backward-compatible default when no
    explicit ``tz`` is threaded in (keeps un-updated callers behaving as before).
    """
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def to_epoch(dt: datetime, tz: Optional[tzinfo] = None) -> float:
    """Naive wall-clock -> epoch seconds, interpreting ``dt`` in ``tz``.

    ``tz`` is the configured system zone (see :mod:`app.config.timezone`);
    when omitted it falls back to the OS-local zone for backward compatibility.
    """
    return dt.replace(tzinfo=tz or _os_local_tz()).timestamp()


def from_epoch(epoch: float, tz: Optional[tzinfo] = None) -> datetime:
    """Epoch seconds -> naive wall-clock in ``tz`` (OS-local when omitted)."""
    return datetime.fromtimestamp(epoch, tz or _os_local_tz()).replace(tzinfo=None)


# ── rrule helpers ────────────────────────────────────────────────────────────

def _build_rule(rrule: str, dtstart_dt: datetime):
    """Return a dateutil rrule bound to ``dtstart_dt``, or None if unavailable."""
    try:
        from dateutil.rrule import rrulestr
    except Exception as exc:  # noqa: BLE001 — dateutil is a core dep; degrade safely
        logger.error(f"[recurrence] python-dateutil unavailable: {exc}")
        return None
    value = (rrule or "").strip()
    if not value:
        return None
    try:
        return rrulestr(value, dtstart=dtstart_dt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[recurrence] failed to parse rrule {value!r}: {exc}")
        return None


def next_occurrence_after(
    *,
    rrule: Optional[str],
    dtstart: str,
    after: datetime,
    until: Optional[str] = None,
) -> Optional[datetime]:
    """The next occurrence strictly after ``after``, or None if exhausted.

    Returns None for a one-shot event (no ``rrule``), when the series' COUNT is
    used up (enforced inside the rrule string), or when the next candidate would
    fall past the ``until`` date bound.
    """
    if not rrule:
        return None  # one-shot: nothing follows
    rule = _build_rule(rrule, parse_local(dtstart))
    if rule is None:
        return None
    nxt = rule.after(after, inc=False)
    if nxt is None:
        return None
    if until:
        if nxt > parse_local(until):
            return None
    return nxt


def first_occurrence_on_or_after(
    *,
    rrule: Optional[str],
    dtstart: str,
    moment: datetime,
    until: Optional[str] = None,
) -> Optional[datetime]:
    """The first occurrence at or after ``moment`` (used to seed next_fire_at).

    For a one-shot (no rrule) this is ``dtstart`` itself if it is at/after
    ``moment``, else None (the event is in the past).
    """
    start = parse_local(dtstart)
    if not rrule:
        return start if start >= moment else None
    rule = _build_rule(rrule, start)
    if rule is None:
        # Treat as one-shot at dtstart so the event still fires once.
        return start if start >= moment else None
    occ = rule.after(moment, inc=True)
    if occ is None:
        return None
    if until and occ > parse_local(until):
        return None
    return occ


def expand_occurrences(
    *,
    rrule: Optional[str],
    dtstart: str,
    range_start: datetime,
    range_end: datetime,
    until: Optional[str] = None,
    hard_cap: int = 366,
) -> List[datetime]:
    """All occurrences within [range_start, range_end] (inclusive), bounded by
    ``hard_cap`` — for rendering a recurrence in the calendar's visible window.

    A one-shot event yields ``[dtstart]`` if it falls inside the range.
    """
    start = parse_local(dtstart)
    if not rrule:
        return [start] if range_start <= start <= range_end else []
    rule = _build_rule(rrule, start)
    if rule is None:
        return [start] if range_start <= start <= range_end else []
    eff_end = range_end
    if until:
        until_dt = parse_local(until)
        if until_dt < eff_end:
            eff_end = until_dt
    out: List[datetime] = []
    for occ in rule.between(range_start, eff_end, inc=True):
        out.append(occ)
        if len(out) >= hard_cap:
            break
    return out
