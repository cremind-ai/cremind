"""Screen composition contract: a tag's active cards -> one logical screen.

Implemented by `app.tags.runtime.compose.screen.compose_screen`; called by the daemon
(`app.tags.runtime.daemon`) every time a tag's active card set or another input of its
screen changes (never for the clock: the masthead's time is when the screen was composed).
See docs/tags/connector-api.md "Screen model" and docs/tags/layout.md. Also in
`compose.screen`: ``compose_identify(panel, fonts, tag_id=None)``,
``compose_setup_code(panel, fonts, code, qr_text)`` and ``compose_blank(panel)``;
previews in `compose.preview` (``preview_png``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.tags.runtime.fonts.fontset import FontSet

COMPOSER_VERSION = 2
"""The look of the screens the composer draws. Bump it in the same commit as any change to how screens look
(layout, type, spacing, colour, wording): after the upgrade every tag then redraws once, by itself
(``QueueStore.redraw_if_changed`` in ``app.tags.runtime.daemon.store``; docs/tags/runtime.md §5 "Redraw once")."""


@dataclass(frozen=True)
class TagPanel:
    """What layout needs to know about a tag's display (from enrollment/sync)."""

    tag_id: int
    width: int
    """Native panel width in pixels."""
    height: int
    planes: int
    """1 = black/white, 2 = black/white/red."""
    plane_flags: int
    rotation: int = 0
    """0..3 quarter turns clockwise, logical -> native (docs/protocol.md §4.4)."""
    name: str = ""


@dataclass(frozen=True)
class ActiveCard:
    """One card the tag should currently show (a delivery job's `card`, see docs/tags/connector-api.md)."""

    delivery_id: int
    kind: str
    priority: int
    created_at: datetime
    card: dict[str, Any]


@dataclass(frozen=True)
class ScreenSettings:
    """Profile display settings delivered by `sync` (docs/tags/connector-api.md)."""

    show_excerpts: bool = False
    qr_links: bool = False
    timezone: str = "UTC"
    language: str = "en"


@dataclass(frozen=True)
class ComposedScreen:
    layout: bytes
    """Encoded logical screen (docs/protocol.md §4), already validated."""
    delivery_ids: tuple[int, ...]
    """Deliveries whose cards this screen shows (all become `displayed` together), in display order
    (the hero card first, then the rows). Cards only counted under "+N MORE" are NOT included
    (docs/tags/layout.md "Delivery ids")."""
    pending_count: int
    """Active cards not shown for lack of space (the N of "+N MORE")."""
    unsupported_chars: tuple[str, ...] = field(default=())
    """Characters no face in the pack covers (reported in previews and diagnostics)."""
    pending_delivery_ids: tuple[int, ...] = field(default=())
    """The deliveries counted under "+N MORE" (``len == pending_count``); they stay undisplayed."""


class Composer(Protocol):
    def __call__(self, panel: TagPanel, cards: list[ActiveCard], fonts: FontSet,
                 settings: ScreenSettings, now: datetime) -> ComposedScreen: ...
