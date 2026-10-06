"""Active cards -> what a screen shows (docs/tags/layout.md "Screen model").

The card JSON is the connector's (docs/tags/connector-api.md "Job shape"). This
module decides, per card, the icon, the plain-text title and body, the
language, the time stamp, a progress fraction, a QR link, the tone and label
of its status chip, and the display order. Nothing here draws.

Policy:

- ``resolved`` and ``clear`` cards are instructions, never shown;
- the icon is ``card.icon`` when it names a built-in icon, else the kind's
  default;
- the body is shown only when the profile enables excerpts
  (``show_excerpts``) and the kind is in `BODY_KINDS`; a ``pinned_note`` body
  is text the owner wrote for this tag and is always shown;
- the **tone** (`tone_of`): ``alert`` for ``needs_input`` cards and ``error``
  severity (red chips and markers on two-plane panels, bold row titles),
  ``caution`` for ``warning`` and ``attention`` (an outlined chip), else
  ``neutral``; ``red`` is ``tone == "alert"``;
- the **label** (`label_of`, English caps from `compose.strings`): what the
  chip or eyebrow says — NEEDS YOU, FAILED / ERROR, WARNING, IMPORTANT, DONE,
  else the kind's word (NOTICE, REPLY, …);
- progress needs real counts: integers ``done >= 0`` and ``total > 0``;
- a QR link must be a short, token-free ``https`` URL (`qr_link`).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from app.tags.runtime.compose import strings
from app.tags.runtime.compose.api import ActiveCard, ScreenSettings
from app.tags.runtime.compose.timefmt import parse_timestamp
from app.tags.runtime.layout.plaintext import plain_text
from app.tags.runtime.layout.unicode import normalize_language
from app.tags.runtime.protocol.ids import LAYOUT_QR_MAX_TEXT, Icon

HIDDEN_KINDS = frozenset({"resolved", "clear"})
BODY_KINDS = frozenset({"excerpt", "needs_input", "task_outcome", "notification", "health", "indexing_problem",
                        "calendar", "automation", "usage", "tag_diagnostics", "progress"})
ALWAYS_BODY_KINDS = frozenset({"pinned_note"})
RED_KINDS = frozenset({"needs_input"})
RED_SEVERITIES = frozenset({"error"})
SEVERITIES = frozenset({"info", "success", "attention", "warning", "error"})
"""The connector's severities (``app.tags.cards``); anything else counts as ``info``."""
CAUTION_SEVERITIES = frozenset({"warning", "attention"})
FAILED_KINDS = frozenset({"task_outcome", "automation", "progress"})
"""Kinds whose ``error`` says a run failed (label FAILED rather than ERROR)."""
ALERT, CAUTION, NEUTRAL = "alert", "caution", "neutral"

KIND_ICONS: dict[str, Icon] = {
    "notification": Icon.NOTIFICATIONS, "task_outcome": Icon.TASK, "needs_input": Icon.HELP, "excerpt": Icon.CHAT,
    "progress": Icon.SYNC, "health": Icon.WARNING, "indexing_problem": Icon.FOLDER, "calendar": Icon.EVENT,
    "automation": Icon.SCHEDULE, "usage": Icon.BAR_CHART, "pinned_note": Icon.PUSH_PIN,
    "tag_diagnostics": Icon.BATTERY_LOW,
}
SEVERITY_ICONS: dict[str, Icon] = {"error": Icon.ERROR, "warning": Icon.WARNING, "success": Icon.CHECK_CIRCLE}

TITLE_MAX = 400
BODY_MAX = 1200
_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{24,}")
_UUID = re.compile(r"(?<![A-Za-z0-9_\-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                   r"(?![A-Za-z0-9_\-])")
_SECRET_WORDS = ("token", "secret", "passw", "apikey", "api_key", "api-key", "access_key", "signature", "jwt",
                 "bearer")


@dataclass(frozen=True)
class CardView:
    """One displayable card, normalised."""

    delivery_id: int
    kind: str
    priority: int
    created_at: datetime
    title: str
    body: str | None
    language: str
    icon: int
    red: bool
    """``tone == "alert"``: drawn with red accents on two-plane panels."""
    ts: datetime
    progress: tuple[int, int] | None
    link: bytes | None
    severity: str = "info"
    """The card's severity, normalised (`SEVERITIES`; unknown -> ``info``)."""
    tone: str = NEUTRAL
    """``alert`` | ``caution`` | ``neutral`` (`tone_of`)."""
    label: str = strings.LABEL_UPDATE
    """The chip / eyebrow word in English caps (`label_of`)."""


def severity_of(card: dict) -> str:
    severity = card.get("severity")
    return severity if isinstance(severity, str) and severity in SEVERITIES else "info"


def tone_of(kind: str, severity: str) -> str:
    """``alert`` (needs_input, error), ``caution`` (warning, attention) or ``neutral``."""
    if kind in RED_KINDS or severity in RED_SEVERITIES:
        return ALERT
    return CAUTION if severity in CAUTION_SEVERITIES else NEUTRAL


def label_of(kind: str, severity: str) -> str:
    """The chip word: the kind's urgency first, then the severity, then the kind (module docstring)."""
    if kind in RED_KINDS:
        return strings.LABEL_NEEDS_YOU
    if severity == "error":
        return strings.LABEL_FAILED if kind in FAILED_KINDS else strings.LABEL_ERROR
    if severity == "warning":
        return strings.LABEL_WARNING
    if severity == "attention":
        return strings.LABEL_IMPORTANT
    if severity == "success":
        return strings.LABEL_DONE
    return strings.KIND_LABELS.get(kind, strings.LABEL_UPDATE)


def icon_for(card: dict, kind: str) -> int:
    name = card.get("icon")
    if isinstance(name, str) and name.upper() in Icon.__members__:
        return int(Icon[name.upper()])
    if kind in KIND_ICONS:
        return int(KIND_ICONS[kind])
    severity = card.get("severity")
    return int(SEVERITY_ICONS.get(severity, Icon.INFO)) if isinstance(severity, str) else int(Icon.INFO)


def progress_of(card: dict) -> tuple[int, int] | None:
    p = card.get("progress")
    if not isinstance(p, dict):
        return None
    done, total = p.get("done"), p.get("total")
    if type(done) is not int or type(total) is not int or total <= 0 or done < 0:
        return None
    return min(done, total), total


def qr_link(link: object) -> bytes | None:
    """``link`` when it is a short, token-free https URL a QR may carry; else None.

    Rules: printable ASCII only, at most ``LAYOUT_QR_MAX_TEXT`` bytes, scheme
    ``https`` with a host (and a valid port, if any), no user info, no query
    (not even an empty ``?``), no ``=``, ``&`` or ``;`` anywhere (parameters in
    paths or fragments), no path or fragment run that looks like a token (24+
    characters of ``[A-Za-z0-9_-]`` once UUID record ids are set aside) and
    none of the usual credential words.
    """
    if not isinstance(link, str) or not 1 <= len(link) <= LAYOUT_QR_MAX_TEXT:
        return None
    if any(not 0x21 <= ord(c) <= 0x7E for c in link):
        return None
    try:
        u = urllib.parse.urlsplit(link)
        _ = u.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if u.scheme.lower() != "https" or not u.hostname or u.username is not None or u.password is not None:
        return None
    if "?" in link or "@" in u.netloc or u.query or any(ch in link for ch in "=&;"):
        return None
    lowered = link.lower()
    if any(word in lowered for word in _SECRET_WORDS):
        return None
    rest = _UUID.sub("/", u.path + "#" + u.fragment)  # record ids (UUIDs) are identifiers, not tokens
    if _TOKENISH.search(rest):
        return None
    return link.encode("ascii")


def card_view(active: ActiveCard, settings: ScreenSettings) -> CardView | None:
    """The displayable view of ``active``, or None for instruction kinds (resolved, clear)."""
    card = active.card if isinstance(active.card, dict) else {}
    kind = active.kind or str(card.get("kind") or "")
    if kind in HIDDEN_KINDS:
        return None
    title = plain_text(str(card.get("title") or "")[:TITLE_MAX])
    if not title:
        title = kind.replace("_", " ").capitalize() or "Update"
    body = None
    raw_body = card.get("body")
    if isinstance(raw_body, str) and raw_body.strip() and (
            kind in ALWAYS_BODY_KINDS or (settings.show_excerpts and kind in BODY_KINDS)):
        body = plain_text(raw_body[:BODY_MAX], keep_newlines=True) or None
    language = normalize_language(card.get("lang")) or normalize_language(settings.language)
    severity = severity_of(card)
    tone = tone_of(kind, severity)
    ts = parse_timestamp(card.get("ts")) or parse_timestamp(active.created_at) or active.created_at
    link = qr_link(card.get("link")) if settings.qr_links else None
    return CardView(active.delivery_id, kind, int(active.priority), active.created_at, title, body, language,
                    icon_for(card, kind), tone == ALERT, ts, progress_of(card) if kind == "progress" else None, link,
                    severity, tone, label_of(kind, severity))


def _sort_key(view: CardView) -> tuple[int, float, int]:
    created = parse_timestamp(view.created_at) or view.ts
    return (-view.priority, -created.timestamp(), -view.delivery_id)


def ordered_views(cards: list[ActiveCard], settings: ScreenSettings) -> list[CardView]:
    """Displayable cards, highest priority first, then newest (then highest delivery id)."""
    views = [v for v in (card_view(c, settings) for c in cards) if v is not None]
    return sorted(views, key=_sort_key)
