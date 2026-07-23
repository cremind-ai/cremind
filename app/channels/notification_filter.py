"""Notification-mode filtering + formatting (pure, stateless).

A channel in ``mode == "notification"`` relays entries from the profile
notifications bus (:mod:`app.events.notifications_bus`) to the user's chat
instead of holding a conversation. This module owns the pieces that need no
adapter state:

- :func:`normalize_notification_filter` — strict validation/coercion used at
  write time (API create/update). Raises :class:`ValueError` on bad input so
  the caller can return HTTP 400.
- :class:`NotificationFilter` + :meth:`NotificationFilter.parse` — lenient
  runtime parse (never raises; a bad stored filter must not crash the adapter)
  and :meth:`NotificationFilter.matches` deciding whether an entry passes.
- :func:`format_notification` — render an entry as a chat message.

Entry shape: produced by
:meth:`app.events.notifications_buffer.EventNotificationsBuffer.push`, which
**flattens** the ``extra`` dict onto the entry — so ``source_kind``,
``subscription_id`` and ``conversation_id`` are read at the entry's top level,
not under an ``extra`` key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config.timezone import resolve_tzinfo

# ── Vocabulary ────────────────────────────────────────────────────────────────

# The notification ``kind`` values Cremind emits today (see the push sites in
# app/agent/stream_runner.py, app/channels/base.py, app/skills/sync.py). Not an
# allowlist for filtering — new kinds are accepted forward-compatibly — but the
# UI offers these as checkboxes.
KNOWN_KINDS = (
    "started",
    "completed",
    "error",
    "channel_otp",
    "skill_register_required",
    "event_run_pending",
    "event_run_completed",
    "event_run_failed",
    "autostart_failed",
    "channel_disabled",
)

# Closed set: the three trigger engines behind an event run.
KNOWN_SOURCE_KINDS = ("skill_event", "file_watcher", "schedule")

# ``channel_otp`` relays another channel's one-time login code; forwarding it
# into a chat would defeat that channel's OTP gate. Always dropped, regardless
# of the user's filter — see the hard guard in :meth:`NotificationFilter.matches`.
OTP_KIND = "channel_otp"

# Default denylist when the user has not configured ``exclude_kinds``: suppress
# the noisy per-run ``started`` ping and never relay OTP codes.
DEFAULT_EXCLUDE_KINDS = ("started", OTP_KIND)

_MIN_PRIORITIES = ("all", "high")
_KEYWORDS_MODES = ("any", "all")
_MAX_LIST = 100
_MAX_KEYWORD_LEN = 200
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ── Normalization / validation ─────────────────────────────────────────────────

def _as_str_list(value: Any, *, strict: bool, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Tolerate a comma-separated string (mirrors the event_types/extensions
        # storage convention) as well as a JSON array.
        items = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(p).strip() for p in value]
    else:
        if strict:
            raise ValueError(f"{label} must be a list of strings")
        return []
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if not it or it in seen:
            continue
        seen.add(it)
        out.append(it)
        if len(out) >= _MAX_LIST:
            break
    return out


def _normalize_quiet_hours(raw: Any, *, strict: bool) -> dict:
    disabled = {
        "enabled": False,
        "start": "22:00",
        "end": "07:00",
        "tz": "",
        "allow_high": True,
    }
    if not isinstance(raw, dict):
        if raw is None:
            return disabled
        if strict:
            raise ValueError("quiet_hours must be an object")
        return disabled

    enabled = bool(raw.get("enabled", False))
    start = str(raw.get("start") or "22:00").strip()
    end = str(raw.get("end") or "07:00").strip()
    tz = str(raw.get("tz") or "").strip()
    allow_high = bool(raw.get("allow_high", True))

    for label, val in (("start", start), ("end", end)):
        if not _HHMM_RE.match(val):
            if strict:
                raise ValueError(f"quiet_hours.{label} must be HH:MM (24h), got {val!r}")
            return disabled
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, KeyError, ModuleNotFoundError):
            if strict:
                raise ValueError(f"quiet_hours.tz is not a valid IANA timezone: {tz!r}")
            tz = ""
    return {
        "enabled": enabled,
        "start": start,
        "end": end,
        "tz": tz,
        "allow_high": allow_high,
    }


def normalize_notification_filter(raw: Any) -> dict:
    """Strictly validate + normalize a notification filter for persistence.

    Raises :class:`ValueError` (with a user-facing message) on invalid input;
    the API maps that to HTTP 400. Unknown top-level keys are dropped. Returns
    the canonical dict stored under ``config["notification_filter"]``.
    """
    return _normalize(raw, strict=True)


def _normalize(raw: Any, *, strict: bool) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("notification_filter must be an object")
        raw = {}

    min_priority = str(raw.get("min_priority") or "all").strip().lower()
    if min_priority not in _MIN_PRIORITIES:
        if strict:
            raise ValueError(f"min_priority must be one of {_MIN_PRIORITIES}")
        min_priority = "all"

    keywords_mode = str(raw.get("keywords_mode") or "any").strip().lower()
    if keywords_mode not in _KEYWORDS_MODES:
        if strict:
            raise ValueError(f"keywords_mode must be one of {_KEYWORDS_MODES}")
        keywords_mode = "any"

    source_kinds = _as_str_list(raw.get("source_kinds"), strict=strict, label="source_kinds")
    if strict:
        bad = [s for s in source_kinds if s not in KNOWN_SOURCE_KINDS]
        if bad:
            raise ValueError(
                f"source_kinds may only contain {KNOWN_SOURCE_KINDS}; got {bad}"
            )
    else:
        source_kinds = [s for s in source_kinds if s in KNOWN_SOURCE_KINDS]

    # exclude_kinds defaults only when the key is entirely absent; an explicit
    # empty list means "exclude nothing".
    if "exclude_kinds" in raw:
        exclude_kinds = _as_str_list(raw.get("exclude_kinds"), strict=strict, label="exclude_kinds")
    else:
        exclude_kinds = list(DEFAULT_EXCLUDE_KINDS)

    keywords = [
        k[:_MAX_KEYWORD_LEN]
        for k in _as_str_list(raw.get("keywords"), strict=strict, label="keywords")
    ]

    return {
        "version": 1,
        "min_priority": min_priority,
        "kinds": _as_str_list(raw.get("kinds"), strict=strict, label="kinds"),
        "exclude_kinds": exclude_kinds,
        "source_kinds": source_kinds,
        "subscription_ids": _as_str_list(
            raw.get("subscription_ids"), strict=strict, label="subscription_ids"
        ),
        "conversation_ids": _as_str_list(
            raw.get("conversation_ids"), strict=strict, label="conversation_ids"
        ),
        "keywords": keywords,
        "keywords_mode": keywords_mode,
        "quiet_hours": _normalize_quiet_hours(raw.get("quiet_hours"), strict=strict),
    }


def default_filter() -> dict:
    """The normalized filter applied when the user configured none."""
    return _normalize({}, strict=False)


# ── Runtime filter ─────────────────────────────────────────────────────────────

@dataclass
class NotificationFilter:
    min_priority: str = "all"
    kinds: list[str] = field(default_factory=list)
    exclude_kinds: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_KINDS))
    source_kinds: list[str] = field(default_factory=list)
    subscription_ids: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    keywords_mode: str = "any"
    quiet_hours: dict = field(default_factory=lambda: _normalize_quiet_hours(None, strict=False))

    @classmethod
    def parse(cls, config: dict | None) -> "NotificationFilter":
        """Lenient parse from ``channel.config`` — never raises."""
        raw = (config or {}).get("notification_filter")
        data = _normalize(raw, strict=False)
        return cls(
            min_priority=data["min_priority"],
            kinds=data["kinds"],
            exclude_kinds=data["exclude_kinds"],
            source_kinds=data["source_kinds"],
            subscription_ids=data["subscription_ids"],
            conversation_ids=data["conversation_ids"],
            keywords=data["keywords"],
            keywords_mode=data["keywords_mode"],
            quiet_hours=data["quiet_hours"],
        )

    def matches(self, entry: dict, *, now: datetime | None = None) -> bool:
        """Return whether ``entry`` should be delivered.

        AND across dimensions; OR within a dimension's list (empty list = no
        constraint). ``now`` is injectable for testing quiet hours; it defaults
        to delivery-time wall clock in the configured timezone.
        """
        kind = entry.get("kind") or ""

        # 1. Hard OTP guard — non-configurable.
        if kind == OTP_KIND:
            return False

        # 2. Kind allow/deny.
        if self.kinds:
            if kind not in self.kinds:
                return False
        elif kind in self.exclude_kinds:
            return False

        # 3. Priority.
        if self.min_priority == "high" and (entry.get("priority") or "normal") != "high":
            return False

        # 4. Source kind (only present on event_run_* entries).
        if self.source_kinds and (entry.get("source_kind") or "") not in self.source_kinds:
            return False

        # 5. Specific automation / conversation.
        if self.subscription_ids and (entry.get("subscription_id") or "") not in self.subscription_ids:
            return False
        if self.conversation_ids and (entry.get("conversation_id") or "") not in self.conversation_ids:
            return False

        # 6. Keywords over title + preview.
        if self.keywords:
            haystack = (
                f"{entry.get('conversation_title') or ''} "
                f"{entry.get('message_preview') or ''}"
            ).lower()
            needles = [k.lower() for k in self.keywords]
            hit = (all if self.keywords_mode == "all" else any)(
                n in haystack for n in needles
            )
            if not hit:
                return False

        # 7. Quiet hours (evaluated last so a cheap reject wins first).
        if self._in_quiet_hours(now=now):
            if not (self.quiet_hours.get("allow_high") and entry.get("priority") == "high"):
                return False

        return True

    def _in_quiet_hours(self, *, now: datetime | None) -> bool:
        qh = self.quiet_hours or {}
        if not qh.get("enabled"):
            return False
        start = _parse_hhmm(qh.get("start"))
        end = _parse_hhmm(qh.get("end"))
        if start is None or end is None or start == end:
            return False
        if now is None:
            tz = _safe_zone(qh.get("tz"))
            now = datetime.now(tz) if tz else datetime.now().astimezone()
        cur = now.time().replace(second=0, microsecond=0)
        if start < end:
            return start <= cur < end
        # Window crosses midnight (e.g. 22:00–07:00).
        return cur >= start or cur < end


def _parse_hhmm(value: Any) -> dt_time | None:
    if not isinstance(value, str) or not _HHMM_RE.match(value.strip()):
        return None
    h, m = value.strip().split(":")
    return dt_time(hour=int(h), minute=int(m))


def _safe_zone(tz: Any) -> ZoneInfo | None:
    if not tz or not isinstance(tz, str):
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError, ModuleNotFoundError):
        return None


# ── Formatting ─────────────────────────────────────────────────────────────────

_EMOJI_BY_KIND = {
    "completed": "✅",
    "event_run_completed": "✅",
    "error": "❌",
    "event_run_failed": "❌",
    "event_run_pending": "❓",
    "skill_register_required": "🔧",
    "started": "▶️",
    "autostart_failed": "⚠️",
    "channel_disabled": "⚠️",
}

_SOURCE_LABEL = {
    "schedule": "Schedule",
    "file_watcher": "File watcher",
    "skill_event": "Skill event",
}

_PREVIEW_LIMIT = 500


def format_notification(entry: dict) -> str:
    """Render a notification entry as a Telegram-markdown chat message.

    Kept resilient to stray markdown: adapters send through ``_send_chunked`` /
    ``_send_with_retry`` which retry as plain text on a markdown parse error.
    """
    kind = entry.get("kind") or ""
    high = entry.get("priority") == "high"
    emoji = _EMOJI_BY_KIND.get(kind, "🔔")
    header = f"🔴 {emoji}" if high else emoji

    title = (entry.get("conversation_title") or "Notification").strip()
    lines = [f"{header} *{title}*"]

    preview = (entry.get("message_preview") or "").strip()
    if preview:
        lines.append(_truncate(preview, _PREVIEW_LIMIT))

    meta_parts: list[str] = []
    source = _SOURCE_LABEL.get(entry.get("source_kind") or "")
    if source:
        meta_parts.append(source)
    ts = _format_time(entry.get("created_at"), resolve_tzinfo(entry.get("profile")))
    if ts:
        meta_parts.append(ts)
    if meta_parts:
        lines.append(f"_{' · '.join(meta_parts)}_")

    return "\n".join(lines)


def _format_time(created_at: Any, tz: tzinfo | None = None) -> str:
    try:
        # created_at is epoch milliseconds (time.time() * 1000). ``tz`` is the
        # profile's resolved zone (see resolve_tzinfo); None falls back to the OS
        # process zone (UTC on Docker/VPS), which is exactly the bug this avoids
        # when a caller passes the profile zone.
        return datetime.fromtimestamp(float(created_at) / 1000, tz=tz).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
