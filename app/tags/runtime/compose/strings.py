"""The composer's own words: every English string a tag screen draws (docs/tags/layout.md "Chrome strings").

Card titles and bodies are the user's text; everything else on a screen comes
from here, so wording is reviewed (and one day translated) in one place. The
strings are English and drawn as left-to-right paragraphs; the values put into
them (times, counts) are localised by the caller. Labels are written in caps:
they are drawn as they are, with letter-spacing (`tokens.TextRole.tracking`).
"""

from __future__ import annotations

UPDATED = "Updated {time}"
"""Header, end side: when the screen was composed (a screen is recomposed only when its cards change)."""
TAG_FALLBACK = "TAG {id}"
"""Header, start side, for a tag without a name (``id`` = 8 hex digits)."""
MORE = "+{n} MORE"
"""The last list slot: cards counted but not shown (their deliveries stay pending)."""
NO_UPDATES = "No updates"
CREMIND_TAG = "CREMIND TAG"
"""Identify screen, above the tag id."""
ADD_THIS_TAG = "ADD THIS TAG"
"""Setup-code screen (a released tag's last screen), above the code."""
ADD_TAG_HINT = "Cremind › Settings › Tags › Add tag"
"""Setup-code screen: where the code is typed ("Cremind › Settings › Tags › Add tag"; the no-break spaces let it
wrap only after a "›")."""

LABEL_NEEDS_YOU = "NEEDS YOU"
LABEL_FAILED = "FAILED"
LABEL_ERROR = "ERROR"
LABEL_WARNING = "WARNING"
LABEL_IMPORTANT = "IMPORTANT"
LABEL_DONE = "DONE"
LABEL_UPDATE = "UPDATE"

KIND_LABELS: dict[str, str] = {
    "notification": "NOTICE",
    "excerpt": "REPLY",
    "task_outcome": LABEL_UPDATE,
    "calendar": "CALENDAR",
    "automation": "AUTOMATION",
    "usage": "USAGE",
    "progress": "RUNNING",
    "pinned_note": "NOTE",
    "health": "HEALTH",
    "indexing_problem": "FILES",
    "tag_diagnostics": "TAG",
}
"""The label of an ``info`` card, by kind (an unknown kind says `LABEL_UPDATE`)."""


def updated(time: str) -> str:
    return UPDATED.format(time=time)


def tag_fallback(tag_id: int) -> str:
    return TAG_FALLBACK.format(id=f"{tag_id:08X}")


def more(n: int) -> str:
    return MORE.format(n=n)
