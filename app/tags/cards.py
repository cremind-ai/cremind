"""Journal event -> normalised card(s): the ``card`` object of a delivery job.

Card shape (``v`` 1, see connector-api.md "Job shape")::

    {"v": 1, "kind": "needs_input", "severity": "attention", "icon": "help",
     "title": "Approve deployment?", "body": null, "lang": "en",
     "ts": "2026-09-27T10:00:00Z", "progress": null, "link": null,
     "source": {"type": "event_run", "id": "…"}}

``severity`` ∈ info | success | attention | warning | error; ``icon`` is a name
from the companion's built-in icon set (``protocol/spec.yaml`` ``icons``).
Everything shown was already sanitised when the entry was journalled
(:mod:`app.tags.sanitize`); this module only picks wording, priority and the
``replace_key`` / ``resolves`` that let a newer card replace or retire an
older one on the tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Kinds of the connector job / card (connector-api.md).
CARD_KINDS = (
    "notification", "task_outcome", "needs_input", "excerpt", "progress", "health",
    "indexing_problem", "calendar", "automation", "usage", "pinned_note",
    "tag_diagnostics", "resolved", "clear",
)
# Delivered to a tag even while it waits for its screen to be cleared.
NON_CONTENT_KINDS = ("clear",)

ICONS = (
    "info", "check_circle", "error", "warning", "help", "chat", "task", "schedule",
    "event", "notifications", "sync", "battery_full", "battery_low", "wifi_off",
    "link_off", "description", "bolt", "push_pin", "bar_chart", "approval",
    "hourglass", "bug_report", "folder", "person",
)

PRIORITY = {
    "clear": 100,
    "needs_input": 90,
    "resolved": 90,
    "health": 75,
    "task_outcome": 50,
    "excerpt": 50,
    "pinned_note": 55,
    "notification": 40,
    "indexing_problem": 45,
    "automation": 35,
    "calendar": 35,
    "progress": 30,
    "tag_diagnostics": 20,
    "usage": 10,
}


def iso(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CardSpec:
    """One card to route. ``target_device_id`` pins it to one tag; otherwise
    :mod:`app.tags.routing` picks the tags (``resolved`` follows the card it
    resolves)."""

    kind: str
    card: dict[str, Any]
    priority: int
    expires_at: float
    replace_key: str | None = None
    resolves: str | None = None
    target_device_id: str | None = None


def make_card(
    kind: str, *, title: str, severity: str = "info", icon: str = "info",
    body: str | None = None, lang: str = "en", ts_ms: float | None = None,
    progress: dict[str, int] | None = None, link: str | None = None,
    source_type: str | None = None, source_id: str | None = None,
) -> dict[str, Any]:
    return {
        "v": 1,
        "kind": kind,
        "severity": severity,
        "icon": icon if icon in ICONS else "info",
        "title": (title or "")[:160],
        "body": (body[:600] if body else None),
        "lang": lang or "en",
        "ts": iso(ts_ms),
        "progress": progress,
        "link": link,
        "source": {"type": source_type, "id": source_id},
    }


def _link(options: dict[str, Any], profile: str, target: str) -> str | None:
    if not options.get("qr_links"):
        return None
    try:
        from app.config.settings import BaseConfig

        base = (getattr(BaseConfig, "APP_URL", "") or "").rstrip("/")
    except Exception:  # noqa: BLE001
        base = ""
    return f"{base}/#/{profile}/{target}" if base else None


def cards_for_event(event: dict[str, Any], *, profile: str, options: dict[str, Any]) -> list[CardSpec]:
    """Cards for one ``tag_events`` row. Unknown kinds produce none."""
    kind = event.get("kind")
    p = event.get("payload") or {}
    lang = options.get("language") or "en"
    ts = event.get("created_at")
    expires = float(event.get("expires_at") or ts or 0)
    src_type = event.get("source_type")
    src_id = event.get("source_id")
    key = event.get("replace_key")

    def spec(card_kind: str, *, title: str, severity: str = "info", icon: str = "info",
             body: str | None = None, progress: dict | None = None, link: str | None = None,
             replace_key: str | None = key, resolves: str | None = None,
             priority: int | None = None, target: str | None = None) -> CardSpec:
        return CardSpec(
            kind=card_kind,
            card=make_card(card_kind, title=title, severity=severity, icon=icon, body=body,
                           lang=lang, ts_ms=ts, progress=progress, link=link,
                           source_type=src_type, source_id=src_id),
            priority=PRIORITY[card_kind] if priority is None else priority,
            expires_at=expires,
            replace_key=replace_key,
            resolves=resolves,
            target_device_id=target,
        )

    def resolved(resolves: str, title: str = "Resolved") -> CardSpec:
        return spec("resolved", title=title, icon="check_circle", replace_key=None, resolves=resolves)

    if kind == "assistant.result":
        if p.get("cancelled"):
            return []
        cid = p.get("conversation_id") or ""
        link = _link(options, profile, f"c/{cid}")
        title = p.get("title") or "Chat"
        if p.get("errored"):
            return [spec("task_outcome", title=f"Reply failed: {title}", severity="error",
                         icon="error", body=p.get("excerpt") or None, link=link, priority=70)]
        if options.get("show_excerpts") and p.get("excerpt"):
            return [spec("excerpt", title=title, icon="chat", body=p.get("excerpt"), link=link)]
        return [spec("task_outcome", title=f"Reply ready: {title}", severity="success",
                     icon="chat", link=link)]

    if kind == "chat.needs_input":
        cid = p.get("conversation_id") or ""
        icon = "approval" if p.get("stage") == "awaiting_approval" else "help"
        return [spec("needs_input", title=p.get("question") or "Waiting for you",
                     severity="attention", icon=icon, body=p.get("title") or None,
                     link=_link(options, profile, f"c/{cid}"))]

    if kind == "chat.needs_input_resolved":
        return [resolved(key or f"chat:{p.get('conversation_id')}:input", "Answered")]

    if kind in ("run.started", "run.progress"):
        progress = None
        if kind == "run.progress" and p.get("total"):
            progress = {"done": int(p.get("done") or 0), "total": int(p.get("total") or 0)}
        return [spec("progress", title=p.get("title") or "Automation", icon="sync",
                     body="Running", progress=progress,
                     link=_link(options, profile, "events"))]

    if kind == "run.needs_input":
        return [spec("needs_input", title=p.get("question") or "Waiting for your reply",
                     severity="attention", icon="help", body=p.get("title") or None,
                     link=_link(options, profile, "events"))]

    if kind == "run.resumed":
        return [resolved(key or f"run:{p.get('run_id')}:input", "Answered")]

    if kind in ("run.completed", "run.failed"):
        run_key = key or f"run:{p.get('run_id')}"
        out: list[CardSpec] = []
        if p.get("resolves_input"):
            out.append(resolved(f"{run_key}:input", "Answered"))
        status = p.get("status")
        if status == "cancelled":
            # Stopped on purpose: retire the progress card, announce nothing.
            out.append(resolved(run_key, "Stopped"))
            return out
        if kind == "run.failed":
            out.append(spec("task_outcome", title=p.get("title") or "Automation", severity="error",
                            icon="error", body=p.get("error") or "Failed", priority=70,
                            link=_link(options, profile, "events")))
        else:
            out.append(spec("task_outcome", title=p.get("title") or "Automation", severity="success",
                            icon="check_circle", body="Completed",
                            link=_link(options, profile, "events")))
        return out

    if kind in ("channel.failed", "channel.unlinked"):
        name = p.get("name") or "Channel"
        failed = kind == "channel.failed"
        return [spec("health", title=f"{name} channel {'stopped' if failed else 'unlinked'}",
                     severity="error" if failed else "warning",
                     icon="error" if failed else "link_off", body=p.get("error") or None,
                     link=_link(options, profile, "channels"))]

    if kind == "channel.recovered":
        return [resolved(key or f"channel:{p.get('channel_id')}", "Channel back")]

    if kind == "automation.failed":
        return [spec("automation", title=f"{p.get('name') or 'Automation'} failed", severity="error",
                     icon="error", body=p.get("error") or None, priority=70,
                     link=_link(options, profile, "events"))]

    if kind == "subscription.changed":
        verb = "subscribed to" if p.get("subscribed") else "left"
        return [spec("notification", title=f"{p.get('sender') or 'Someone'} {verb} {p.get('channel_type') or 'a channel'}",
                     icon="person")]

    if kind == "notification":
        high = p.get("priority") == "high"
        return [spec("notification", title=p.get("title") or "Notification",
                     severity="attention" if high else "info", icon="notifications",
                     body=p.get("preview") or None, priority=60 if high else None)]

    if kind == "tag.diagnostics":
        device = p.get("device_id")
        if not device:
            return []
        diag_key = key or f"diag:{device}"
        if p.get("issue") in (None, "", "ok"):
            return [spec("resolved", title="OK", icon="battery_full", replace_key=None,
                         resolves=diag_key, target=device)]
        low = p.get("issue") == "battery_low"
        return [spec("tag_diagnostics",
                     title="Battery low" if low else "Tag not seen for a while",
                     severity="warning", icon="battery_low" if low else "wifi_off",
                     body=p.get("detail") or None, replace_key=diag_key, target=device)]

    if kind in ("calendar.upcoming", "automation.upcoming", "usage.summary",
                "indexing.problem", "health.summary"):
        card_kind = {
            "calendar.upcoming": "calendar",
            "automation.upcoming": "automation",
            "usage.summary": "usage",
            "indexing.problem": "indexing_problem",
            "health.summary": "health",
        }[kind]
        if p.get("empty"):
            return [resolved(key or f"periodic:{card_kind}", "Nothing to show")]
        icon = {"calendar": "event", "automation": "schedule", "usage": "bar_chart",
                "indexing_problem": "folder", "health": "warning"}[card_kind]
        severity = "warning" if card_kind in ("indexing_problem", "health") else "info"
        return [spec(card_kind, title=p.get("title") or card_kind.replace("_", " ").title(),
                     severity=severity, icon=icon, body=p.get("body") or None)]

    return []
