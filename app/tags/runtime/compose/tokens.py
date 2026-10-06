"""Design tokens: the panel classes and every size, space and count a tag screen is drawn with.

A screen's logical canvas falls in one of four **panel classes** by its short
side S (docs/tags/layout.md "Design"):

=====  ===============  ======================================================
XS     S < 160          2.13" 250x122/128, 2.9" 296x128 — landscape or portrait
S      160 <= S < 240   2.7" 264x176, 1.54" 200x200
M      240 <= S < 400   4.2" 400x300 and 300x400
L      S >= 400         7.5" 800x480 — two columns when width >= 600 and
                        width / height >= 1.4, else one (480x800, 2048x2048)
=====  ===============  ======================================================

The look ("Editorial"): one bold hero title is the focal point, everything
else is 12–14 px; hierarchy comes from size and weight, not boxes. A masthead
(tag name in letter-spaced bold caps, "Updated …" at the end side) sits over a
thin accent rule; a status chip, label or icon row (the *eyebrow*) introduces
the hero card; the other cards follow as one-line rows on a fixed pitch with
a square marker (XS) or a 16 px icon (S/M/L).

Text sizes here are **wanted** sizes (`TextRole`): `canvas.Canvas.resolve`
turns a role into a strike the pack has (12 and 14 px become 16 on packs
without them, bold becomes regular), raises it to the text's legibility floor
(`layout.legible_size`) and drops the letter-spacing outside Latin, Greek and
Cyrillic. Distances that depend on a text size (cap height, descender, line
pitch) are measured on the size actually used (`Canvas.metrics`), so the same
tokens lay out every pack.
"""

from __future__ import annotations

from dataclasses import dataclass

PITCH: dict[int, int] = {12: 16, 14: 19, 16: 20, 24: 29, 32: 38}
"""Baseline-to-baseline distance of consecutive lines of one text, by size (tight leading; lines still grow to
their ink when marks need it)."""
TITLE_GAP: dict[int, int] = {16: 4, 24: 6, 32: 8}
"""White rows between the eyebrow and the hero title's cap height, by title size."""

XS, S, M, L = "XS", "S", "M", "L"


def pitch(size: int) -> int:
    """`PITCH` for ``size`` (sizes outside the table: 1.2 x size + 1)."""
    return PITCH.get(size, size + size // 5 + 1)


def title_gap(size: int) -> int:
    return TITLE_GAP.get(size, max(4, size // 4))


@dataclass(frozen=True)
class TextRole:
    """A wanted text style (before `Canvas.resolve`)."""

    size: int
    bold: bool = False
    caps: bool = False
    """Uppercased with ICU in the text's locale (`Canvas.caps`) — composer labels are written in caps already."""
    tracking: int = 0
    """Letter-spacing in px, only for Latin, Greek and Cyrillic text."""


@dataclass(frozen=True)
class TitleStep:
    """One entry of the hero title ladder."""

    size: int
    lines: int
    whole: bool = False
    """Used only when the whole title fits in ``lines`` (no ellipsis)."""


REGULAR_12 = TextRole(12)
CAPS_12 = TextRole(12, bold=True, caps=True, tracking=1)
CAPS_14 = TextRole(14, bold=True, caps=True, tracking=1)

CLOCK_FULL: tuple[tuple[bool, str], ...] = ((True, "MMMEdjm"), (True, "Ejm"), (True, "jm"), (False, "jm"))
"""Header time ladder: (with "Updated", ICU skeleton), the first that leaves the tag name whole wins."""
CLOCK_SHORT: tuple[tuple[bool, str], ...] = CLOCK_FULL[1:]


@dataclass(frozen=True)
class Tokens:
    """Every design decision for one canvas (see the module docstring; values per class in `tokens_for`)."""

    cls: str
    width: int
    height: int
    portrait: bool
    split: bool
    """Two columns (L landscape): the hero on the start side, the list on the end side."""
    margin: int
    gap: int
    """Horizontal gap between neighbours: icon -> chip, text -> time, text column -> QR."""

    # header (masthead)
    name: TextRole
    clock: TextRole
    clock_ladder: tuple[tuple[bool, str], ...]
    cap_top: int
    """First row of the header's capitals."""
    rule: int
    """Accent rule thickness (red on two-plane panels)."""
    rule_gap: int
    """White rows between the header's descender line and the rule."""
    after_rule: int
    """White rows between the rule and the content."""

    # eyebrow
    chip: TextRole
    chip_pad: tuple[int, int]
    """(vertical, horizontal) padding around a chip's label."""
    eyebrow_icon: int
    """Icon size before the chip (0: none)."""
    card_time: TextRole

    # hero
    titles: tuple[TitleStep, ...]
    body: TextRole
    body_lines: tuple[int, int]
    """(minimum reserved before list rows, maximum)."""
    block_gap: int
    """White rows between hero parts (title -> progress -> body)."""
    progress_h: int
    qr_modules: tuple[int, ...]
    """QR module sizes to try, largest first."""
    section_gap: int
    """White rows between the hero's ink and the hairline over the list."""

    # list rows
    row_title: TextRole
    row_time: TextRole | None
    """None: rows show no time (XS portrait)."""
    row_pitch: int
    row_top: int
    """White rows between a slot's top and the row's cap height."""
    marker: int
    """Row marker: a square of this size (XS) or, when 0, a 16 px icon."""
    marker_gap: int
    row_lines: int
    """Lines a row title may take when slots are left over (portrait, L)."""
    meta: TextRole | None
    """L rows: a second line with the label (a mini chip for alert/caution) and the time."""
    rows: tuple[int, int, int]
    """(max, floor, target): list rows at most; the floor every plan keeps before the body minimum goes; the
    rows the hero title leaves room for."""
    more: TextRole

    # other screens
    empty_icon: int
    empty_text: TextRole
    frame: int
    """Identify frame thickness."""
    code: TextRole
    """Setup code size."""

    @property
    def content_width(self) -> int:
        return self.width - 2 * self.margin

    @property
    def row_icon(self) -> int:
        """Icon size of row markers (0 on XS: squares)."""
        return 0 if self.marker else 16


def panel_class(width: int, height: int) -> str:
    short = min(width, height)
    return XS if short < 160 else S if short < 240 else M if short < 400 else L


def tokens_for(width: int, height: int) -> Tokens:
    """The tokens of a ``width`` x ``height`` logical canvas."""
    cls = panel_class(width, height)
    portrait = height > width
    common = dict(cls=cls, width=width, height=height, portrait=portrait)
    if cls == XS:
        return Tokens(**common, split=False, margin=6, gap=6,
                      name=CAPS_12, clock=REGULAR_12, clock_ladder=CLOCK_SHORT, cap_top=4, rule=2, rule_gap=1,
                      after_rule=3,
                      chip=CAPS_12, chip_pad=(2, 4), eyebrow_icon=0, card_time=REGULAR_12,
                      titles=(TitleStep(24, 2, whole=True), TitleStep(16, 6 if portrait else 2)),
                      body=REGULAR_12, body_lines=(1, 4 if portrait else 2), block_gap=4, progress_h=6,
                      qr_modules=(2,), section_gap=2,
                      row_title=REGULAR_12, row_time=None if portrait else REGULAR_12, row_pitch=16, row_top=2,
                      marker=5, marker_gap=5, row_lines=2 if portrait else 1, meta=None,
                      rows=(8, 1, 3) if portrait else (4, 1, 2), more=CAPS_12,
                      empty_icon=24, empty_text=TextRole(14, bold=True), frame=3, code=TextRole(14, bold=True))
    if cls == S:
        return Tokens(**common, split=False, margin=8, gap=6,
                      name=CAPS_12, clock=REGULAR_12, clock_ladder=CLOCK_FULL, cap_top=5, rule=2, rule_gap=1,
                      after_rule=4,
                      chip=CAPS_12, chip_pad=(2, 4), eyebrow_icon=16, card_time=REGULAR_12,
                      titles=(TitleStep(24, 2, whole=True), TitleStep(16, 3)),
                      body=REGULAR_12, body_lines=(1, 4), block_gap=4, progress_h=7, qr_modules=(3, 2),
                      section_gap=3,
                      row_title=TextRole(14), row_time=REGULAR_12, row_pitch=20, row_top=4,
                      marker=0, marker_gap=6, row_lines=1, meta=None, rows=(6, 1, 3), more=CAPS_12,
                      empty_icon=32, empty_text=TextRole(16, bold=True), frame=3, code=TextRole(16, bold=True))
    if cls == M:
        return Tokens(**common, split=False, margin=12, gap=6,
                      name=CAPS_12, clock=REGULAR_12, clock_ladder=CLOCK_FULL, cap_top=8, rule=2, rule_gap=2,
                      after_rule=6,
                      chip=CAPS_12, chip_pad=(3, 5), eyebrow_icon=16, card_time=REGULAR_12,
                      titles=(TitleStep(24, 2, whole=True), TitleStep(16, 3)),
                      body=TextRole(14), body_lines=(2, 6), block_gap=4, progress_h=8, qr_modules=(3, 2),
                      section_gap=6,
                      row_title=TextRole(14), row_time=REGULAR_12, row_pitch=22, row_top=5,
                      marker=0, marker_gap=6, row_lines=2 if portrait else 1, meta=None,
                      rows=(12, 2, 4) if portrait else (8, 2, 4), more=CAPS_12,
                      empty_icon=32, empty_text=TextRole(16, bold=True), frame=4, code=TextRole(24, bold=True))
    return Tokens(**common, split=width >= 600 and width * 5 >= height * 7, margin=16, gap=8,
                  name=CAPS_14, clock=TextRole(14), clock_ladder=CLOCK_FULL, cap_top=15, rule=3, rule_gap=2,
                  after_rule=14,
                  chip=CAPS_14, chip_pad=(4, 6), eyebrow_icon=24, card_time=TextRole(14),
                  titles=(TitleStep(32, 2, whole=True), TitleStep(24, 3)),
                  body=TextRole(16), body_lines=(3, 14), block_gap=6, progress_h=10, qr_modules=(4, 3),
                  section_gap=8,
                  row_title=TextRole(16), row_time=REGULAR_12, row_pitch=44, row_top=7,
                  marker=0, marker_gap=8, row_lines=1, meta=REGULAR_12, rows=(12, 3, 5), more=CAPS_12,
                  empty_icon=48, empty_text=TextRole(24, bold=True), frame=6, code=TextRole(32, bold=True))
