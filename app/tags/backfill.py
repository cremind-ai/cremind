"""Deliver the content a tag missed while it waited for its screen to clear.

While ``clear_required`` is set (a claim until the companion confirms the old
screen was blanked), the projection routes no content to the tag. When the
clear succeeds, :func:`backfill_tag` replays the owner's unexpired journal —
the same cards the projection would have made — and delivers, for that tag:

- every card that describes a CURRENT state and is still open: the latest per
  ``replace_key`` of needs-input, health, indexing, calendar, automation,
  usage, diagnostics (for this tag) and run progress, minus those a later
  ``resolved`` retired — whenever they were journalled;
- every other card journalled since the claim (``since_ms``), latest per
  ``replace_key`` — the notifications and outcomes of the waiting window.

Routing (the owner's settings) applies as usual. Runs inside the caller's
transaction, which already holds the owner's stream-row lock.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.storage.models import TagEventModel
from app.tags import routing
from app.tags.cards import NON_CONTENT_KINDS, cards_for_event
from app.tags.storage import SETTINGS, write_deliveries

EVENTS = TagEventModel.__table__

# Card kinds that describe a state that is still true until retired.
STATE_KINDS = frozenset({
    "needs_input", "health", "indexing_problem", "calendar", "automation", "usage",
    "tag_diagnostics", "progress",
})
_MAX_EVENTS = 2000


async def backfill_tag(conn, profile: str, device: dict[str, Any], now: float, *, since_ms: float) -> int:
    """Write the held cards for ``device``; returns how many."""
    settings = (await conn.execute(select(SETTINGS.c.options).where(SETTINGS.c.profile == profile))).first()
    options = routing.effective_options((settings.options if settings else None) or {},
                                        await routing.admin_defaults())
    events = (await conn.execute(
        select(EVENTS).where(EVENTS.c.profile == profile, EVENTS.c.expires_at > now)
        .order_by(EVENTS.c.seq.desc()).limit(_MAX_EVENTS)
    )).all()
    latest: dict[str, tuple[int, Any, str]] = {}
    unkeyed: list[tuple[int, Any, str]] = []
    for ev in reversed(events):
        event = dict(ev._mapping)
        recent = float(event.get("created_at") or 0) >= since_ms
        for spec in cards_for_event(event, profile=profile, options=options):
            if spec.expires_at <= now or spec.kind in NON_CONTENT_KINDS:
                continue
            if spec.target_device_id and spec.target_device_id != device["id"]:
                continue
            if spec.kind == "resolved":
                latest.pop(spec.resolves or "", None)
                continue
            if not spec.target_device_id and not routing.route_targets(spec.kind, options, [device]):
                continue
            if not (spec.kind in STATE_KINDS or recent):
                if spec.replace_key:
                    latest.pop(spec.replace_key, None)  # an older state card it replaced
                continue
            if spec.replace_key:
                latest.pop(spec.replace_key, None)
                latest[spec.replace_key] = (int(event["seq"]), spec, event["id"])
            else:
                unkeyed.append((int(event["seq"]), spec, event["id"]))
    picked = sorted(list(latest.values()) + unkeyed, key=lambda item: item[0])
    if not picked:
        return 0
    await write_deliveries(conn, profile, [(device, spec, event_id) for _, spec, event_id in picked], now)
    return len(picked)
