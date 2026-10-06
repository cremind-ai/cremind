"""Screen components: masthead, eyebrow, hero card, list rows, overflow, footer, empty state.

Each component measures first and draws later: ``measure`` decides sizes,
lines and heights for a column (``x`` from the start-side margin, ``w``
wide) and the result's ``cmds``/``build`` give the commands with its top at a
given ``y``, so the composer (`compose.screen`) can try a layout, count its
cost and only then commit it (`Canvas.add`). Vertical positions are
baselines: a part's capitals start ``Metrics.cap`` rows above its baseline,
and every gap is also checked against the real ink of the text drawn
(`Canvas.ink`), so stacked marks, Arabic and CJK push their neighbours away
instead of touching them.

The parts, top to bottom (docs/tags/layout.md "Screen model"):

- **masthead** (`header`): the tag name in bold letter-spaced caps at the
  start side, "Updated {time}" at the end side (the first rung of the
  class's time ladder that leaves the name whole), and the accent rule under
  both — the one red on a calm screen;
- **eyebrow** (`Eyebrow`): [icon (S/M/L)] [chip or label] … [card time]. An
  alert card gets a filled accent chip with its label knocked out in white, a
  caution card a 1 px black outline chip, any other card a plain bold label;
- **hero** (`Hero`): the first card's title in bold (the first rung of the
  title ladder that fits), a progress bar with "7/12", the body (only when
  excerpts are on, or a pinned note) and a QR code at the end side when it
  fits beside the text;
- **rows** (`Row`, `choose_rows`): the next cards, one per slot of a fixed
  pitch — a square marker (XS: filled accent = alert, filled black =
  caution, hollow = neutral) or a 16 px icon (accent for alerts), the title
  (bold for alerts), the time or "7/12" at the end side; a text whose script
  needs a larger size takes more slots; L rows add a meta line (the label, in
  a mini chip for alert and caution cards, then the time);
- **overflow**: "+N MORE" in the last slot when cards are left over;
- **footer** (`Footer`, XS on packs without 12 px text): the time and
  "+N MORE" under a hairline instead of a masthead;
- **empty state** (`empty_cmds`): a check icon and "No updates", centred.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import icu

from app.tags.runtime.compose import strings
from app.tags.runtime.compose.api import ScreenSettings
from app.tags.runtime.compose.canvas import QR_QUIET, Canvas, Style
from app.tags.runtime.compose.cards import ALERT, CAUTION, CardView
from app.tags.runtime.compose.timefmt import card_stamp, format_time
from app.tags.runtime.compose.tokens import TextRole, title_gap
from app.tags.runtime.layout.engine import TextBlock
from app.tags.runtime.layout.unicode import icu_locale
from app.tags.runtime.protocol.ids import Color, Icon, QrEcc
from app.tags.runtime.protocol.layout import Command, LayoutError, qr_code

MIN_TIME_GAP = 8
"""Least white between an eyebrow's label and its time; closer, the time is dropped."""
META_GAP = 6
"""White between a meta line's label and its time (L rows)."""
MINI_CHIP_PAD = (2, 4)
"""(vertical, horizontal) padding of the mini chip on L meta lines."""
MIN_MORE_TEXT = 4
"""A row title takes a second line only when that shows at least this many more characters."""
MIN_ROW_GLYPHS = 8
"""A cut row title must show at least this many characters (spaces aside) to count as shown; with fewer it takes
a second line where the class allows one, else it waits under "+N MORE"."""

_icon_rows: dict[tuple[bytes, int, int], tuple[int, int]] = {}


def number(n: int, language: str) -> str:
    """``n`` with the locale's digits and grouping."""
    return str(icu.NumberFormat.createInstance(icu_locale(language or "en")).format(n))


def progress_text(view: CardView, language: str) -> str:
    assert view.progress is not None
    done, total = view.progress
    return f"{number(done, language)}/{number(total, language)}"


def icon_ink(c: Canvas, icon: int, size: int) -> tuple[int, int]:
    """(first, last + 1) rows with ink of an icon inside its ``size`` px cell."""
    if not size:
        return (0, 0)
    key = (c.fonts.pack_id, size, int(icon))
    rows = _icon_rows.get(key)
    if rows is None:
        glyph = c.ctx.glyph(0, size, int(icon))
        rows = (0, size)
        if glyph is not None and glyph.bitmap:
            row_bytes = (glyph.width + 7) // 8
            inked = [r for r in range(glyph.height) if any(glyph.bitmap[r * row_bytes:(r + 1) * row_bytes])]
            if inked:
                rows = (inked[0], inked[-1] + 1)
        _icon_rows[key] = rows
    return rows


@dataclass
class Drawn:
    """Commands and the text blocks they draw (for `ComposedScreen.unsupported_chars`)."""

    cmds: list[Command] = field(default_factory=list)
    blocks: list[TextBlock] = field(default_factory=list)

    def text(self, c: Canvas, block: TextBlock, x: int, baseline: int, w: int, color: int = Color.BLACK) -> None:
        self.cmds += c.glyph_cmds(block, x, baseline, w, color)
        self.blocks.append(block)

    def extend(self, other: Drawn) -> None:
        self.cmds += other.cmds
        self.blocks += other.blocks


# --------------------------------------------------------------------------- masthead and footer


def header(c: Canvas, now: datetime, settings: ScreenSettings, mode: str) -> int:
    """Draw the masthead (module docstring); returns the first row under the accent rule.

    ``mode`` ``"full"`` walks the class's time ladder, ``"short"`` shows the bare time.
    """
    t = c.tokens
    cw = t.content_width
    name_text = c.caps(c.panel.name.strip()) if c.panel.name.strip() else strings.tag_fallback(c.panel.tag_id)
    name_style = c.resolve(t.name, name_text, keep_bold=True)
    ladder = t.clock_ladder if mode == "full" else t.clock_ladder[-1:]
    name: TextBlock | None = None
    clock: TextBlock | None = None
    name_w = 0
    for with_word, skeleton in ladder:
        when = format_time(now, settings.language, settings.timezone, skeleton)
        text = strings.updated(when) if with_word else when
        clock = c.chrome(text, cw, c.resolve(t.clock, text), align=c.ui_end)
        name_w = cw - clock.width - 2 * t.gap
        name = c.text(name_text, name_w, name_style, align=c.ui_start) if name_w >= 16 else None
        if name is not None and not name.truncated:
            break
    assert clock is not None
    blocks = [b for b in (name, clock) if b is not None]
    inks = [c.ink(b) for b in blocks]
    cap = max(c.metrics(b.size_px).cap for b in blocks)
    desc = max(c.metrics(b.size_px).desc for b in blocks)
    baseline = max(t.cap_top + cap, 2 + max(i.above for i in inks))
    rule_y = max(baseline + desc + t.rule_gap, baseline + max(i.below for i in inks) + 1)
    out = Drawn()
    if name is not None:
        out.text(c, name, 0, baseline, name_w)
    out.text(c, clock, 0, baseline, cw)
    out.cmds += c.rect(0, rule_y, cw, t.rule, c.accent)
    c.add(out.cmds, out.blocks)
    return rule_y + t.rule


@dataclass
class Footer:
    """The bottom row of an XS screen without a masthead: a hairline, "+N MORE" and the time."""

    top: int
    """The hairline's row."""
    baseline: int

    @classmethod
    def measure(cls, c: Canvas) -> Footer:
        m = c.metrics(c.size(c.tokens.clock.size))
        baseline = c.height - 2 - m.desc
        return cls(baseline - m.cap - 4, baseline)

    def cmds(self, c: Canvas, now: datetime, settings: ScreenSettings, pending: int) -> Drawn:
        """The footer row: the first of "+N MORE" / "+N" with "Updated {time}" / "{time}" that fits."""
        t = c.tokens
        cw = t.content_width
        when = format_time(now, settings.language, settings.timezone, "jm")
        mores = [strings.more(pending), f"+{pending}"] if pending else [""]
        clocks = [c.chrome(text, cw, c.resolve(t.clock, text), align=c.ui_end)
                  for text in (strings.updated(when), when)]
        out = Drawn(c.hairline(0, self.top, cw))
        for more_text in mores:
            more = c.chrome(more_text, cw, c.resolve(t.more, more_text), align="left") if more_text else None
            more_w = more.width + 2 * t.gap if more is not None else 0
            clock = next((k for k in clocks if not k.truncated and more_w + k.width <= cw), None)
            if clock is not None or more_text == mores[-1]:
                if more is not None:
                    out.text(c, more, 0, self.baseline, more.width)
                if clock is not None:
                    out.text(c, clock, 0, self.baseline, cw)
                break
        return out


# --------------------------------------------------------------------------- eyebrow


@dataclass
class Eyebrow:
    """[icon] [chip or label] … [time] over the hero title (module docstring)."""

    height: int
    """Rows from the chip's top to the bottom of the eyebrow's ink."""
    top: int
    """First row of the eyebrow's ink relative to the chip's top (< 0: an icon or a tall time reaches above)."""
    build: Callable[[int], Drawn]

    @classmethod
    def measure(cls, c: Canvas, view: CardView, x: int, w: int, now: datetime, settings: ScreenSettings, *,
                ornaments: bool) -> Eyebrow:
        t = c.tokens
        style = c.resolve(t.chip, view.label)
        label = c.chrome(view.label, w, style, align="left")
        m = c.metrics(style.size)
        pad_v, pad_h = t.chip_pad
        boxed = view.tone in (ALERT, CAUTION)
        chip_h = 2 * pad_v + m.cap
        base = pad_v + m.cap
        chip_w = label.width + (2 * pad_h if boxed else 0)
        icon = c.icon_size(t.eyebrow_icon) if ornaments and t.eyebrow_icon else 0
        icon_y = (chip_h - icon) // 2
        ink_top, ink_bottom = icon_ink(c, view.icon, icon)
        chip_x = icon + t.gap if icon else 0
        time: TextBlock | None = None
        for long in (True, False):
            text = card_stamp(view.ts, now, settings.language, settings.timezone, long=long)
            block = c.chrome(text, w, c.resolve(t.card_time, text), align=c.ui_end)
            if chip_x + chip_w + MIN_TIME_GAP + block.width <= w and not block.truncated:
                time = block
                break
        tink = c.ink(time) if time is not None else None
        height = max(chip_h, icon_y + ink_bottom if icon else 0, base + tink.below if tink else 0)
        top = min(0 if boxed else pad_v,  # a plain label's capitals start under the chip's padding
                  icon_y + ink_top if icon else pad_v, base - tink.above if tink else pad_v)

        def build(y: int) -> Drawn:
            out = Drawn()
            out.cmds += c.icon(view.icon, icon, x, y + icon_y, Color.BLACK)
            if view.tone == ALERT:
                out.cmds += c.chip(x + chip_x, y, chip_w, chip_h, c.accent, filled=True, notch=ornaments)
                out.text(c, label, x + chip_x + pad_h, y + base, label.width, Color.WHITE)
            elif view.tone == CAUTION:
                out.cmds += c.chip(x + chip_x, y, chip_w, chip_h, Color.BLACK, filled=False, notch=ornaments)
                out.text(c, label, x + chip_x + pad_h, y + base, label.width)
            else:
                out.text(c, label, x + chip_x, y + base, label.width)
            if time is not None:
                out.text(c, time, x, y + base, w)
            return out

        return cls(height, top, build)


# --------------------------------------------------------------------------- hero


@dataclass
class QrBox:
    """A QR code beside the hero text: ink ``side`` px square, `QR_QUIET` modules of white around it."""

    module: int
    side: int
    top: int
    """Ink top relative to the hero's top."""
    text: bytes

    @property
    def quiet(self) -> int:
        return QR_QUIET * self.module

    @property
    def bottom(self) -> int:
        """End of the quiet zone under the ink (relative)."""
        return self.top + self.side + self.quiet


def fit_qr(c: Canvas, link: bytes, col_w: int, zone_h: int) -> QrBox | None:
    """The largest QR (the class's module sizes) whose ink and quiet zone fit a third of the column and the
    hero zone's height; its ink is aligned with the column's end edge and the eyebrow's top (the margin and
    the gap under the accent rule hold the outer quiet zone)."""
    try:
        modules = qr_code(link, int(QrEcc.LOW)).get_size()
    except LayoutError:
        return None
    t = c.tokens
    for module in t.qr_modules:
        side = modules * module
        quiet = QR_QUIET * module
        top = max(0, quiet - t.after_rule)
        if side + quiet <= col_w // 3 and top + side + quiet <= zone_h:
            return QrBox(module, side, top, link)
    return None


@dataclass
class Hero:
    """The first card: eyebrow, title, progress and QR (its fixed parts), then the body (what is left)."""

    view: CardView
    x: int
    w: int
    """The text column (narrowed by the QR)."""
    col_w: int
    eyebrow: Eyebrow
    title: TextBlock
    title_base: int
    """First title baseline relative to the hero's top (the chip's top)."""
    fixed_bottom: int
    """End of the title / progress ink (relative)."""
    qr: QrBox | None
    progress: tuple[TextBlock, int, int, int] | None = None
    """(label, label baseline, bar y, bar width)."""
    body_style: Style | None = None
    body_base: int = 0
    """First body baseline (relative)."""
    body_reserve: int = 0
    """Body lines the zone keeps for the body (0 when the title needed them)."""

    def body_block(self, c: Canvas, lines: int, *, cut: int | None = None) -> TextBlock:
        assert self.view.body and self.body_style is not None
        return c.text(self.view.body, self.w, self.body_style, language=self.view.language, lines=lines, cut=cut)

    def body_bottom(self, c: Canvas, block: TextBlock | None) -> int:
        """End (relative) of the text column with ``block`` as the body."""
        if block is None or not block.lines or self.body_style is None:
            return self.fixed_bottom
        return self.body_base + c.span(block) + max(c.metrics(self.body_style.size).desc, c.ink(block).below)

    def bottom(self, c: Canvas, block: TextBlock | None) -> int:
        """End (relative) of the whole hero: the text column or the QR's quiet zone."""
        return max(self.body_bottom(c, block), self.qr.bottom if self.qr else 0)

    def body_lines_for(self, c: Canvas, height: int) -> int:
        """Body lines whose metric box ends within ``height`` rows of the hero's top."""
        if self.body_style is None:
            return 0
        m = c.metrics(self.body_style.size)
        room = height - self.body_base - m.desc
        return 0 if room < 0 else 1 + room // m.pitch

    def fixed(self, c: Canvas, y: int) -> Drawn:
        """Eyebrow, title, progress and QR with the hero's top at ``y``."""
        out = self.eyebrow.build(y)
        out.text(c, self.title, self.x, y + self.title_base, self.w)
        if self.progress is not None and self.view.progress is not None:
            label, base, bar_y, bar_w = self.progress
            done, total = self.view.progress
            out.cmds += c.progress(self.x, y + bar_y, bar_w, c.tokens.progress_h, done, total)
            out.text(c, label, self.x, y + base, self.w)
        if self.qr is not None:
            out.cmds += c.qr(self.x + self.col_w - self.qr.side, y + self.qr.top, self.qr.side, self.qr.module,
                             self.qr.text)
        return out

    def body(self, c: Canvas, block: TextBlock, y: int) -> Drawn:
        out = Drawn()
        out.text(c, block, self.x, y + self.body_base, self.w)
        return out

    @classmethod
    def measure(cls, c: Canvas, view: CardView, x: int, col_w: int, zone_h: int, now: datetime,
                settings: ScreenSettings, *, ornaments: bool, title_step: int = 0, title_lines: int | None = None,
                body: bool = True, body_reserve: int = 0, qr: bool = True) -> Hero:
        """Fit the hero into a column ``col_w`` wide and ``zone_h`` rows high (from the chip's top).

        The title takes the first rung of the class ladder (from ``title_step``) that fits while leaving the
        progress row and ``body_reserve`` body lines in the zone — a ``whole`` rung only when the title is not
        cut. The last rung never gives a title line up for the body: at each line count it fits with the body's
        lines or else without them (the body then gets what is left), and only then drops a line (one line at
        least: the caller decides what then). ``title_lines`` caps the last rung's lines.
        """
        t = c.tokens
        box = fit_qr(c, view.link, col_w, zone_h) if qr and view.link and c.limits.qr - c.qrs >= 1 else None
        w = col_w - (box.side + box.quiet + t.gap if box else 0)
        eyebrow = Eyebrow.measure(c, view, x, w, now, settings, ornaments=ornaments)
        body_style = c.resolve(t.body, view.body) if body and view.body else None
        progress_label = None
        if view.progress is not None:
            text = progress_text(view, settings.language)
            progress_label = c.chrome(text, w, c.resolve(t.card_time, text), align=c.ui_end)

        def stack(title: TextBlock, style: Style) -> Hero:
            return cls._stack(c, view, x, w, col_w, eyebrow, title, style, box, progress_label, body_style)

        def fits(hero: Hero, reserve: int = body_reserve) -> bool:
            need = hero.fixed_bottom
            if body_style is not None and reserve:
                need = hero.body_bottom(c, hero.body_block(c, reserve))
            return max(need, box.bottom if box else 0) <= zone_h

        steps = t.titles[min(title_step, len(t.titles) - 1):]
        hero: Hero | None = None
        for i, step in enumerate(steps):
            last = i == len(steps) - 1
            style = c.resolve(TextRole(step.size, bold=True), view.title)
            most = min(step.lines, title_lines) if last and title_lines else step.lines
            if step.whole and not last:
                title = c.text(view.title, w, style, language=view.language, lines=most)
                if not title.truncated and fits(hero := stack(title, style)):
                    return replace(hero, body_reserve=body_reserve)
                continue
            for lines in range(most, 0, -1):
                hero = stack(c.text(view.title, w, style, language=view.language, lines=lines), style)
                if fits(hero):
                    return replace(hero, body_reserve=body_reserve)
                if fits(hero, 0):
                    return hero
        assert hero is not None
        return hero

    @classmethod
    def _stack(cls, c: Canvas, view: CardView, x: int, w: int, col_w: int, eyebrow: Eyebrow, title: TextBlock,
               style: Style, box: QrBox | None, progress_label: TextBlock | None,
               body_style: Style | None) -> Hero:
        t = c.tokens
        m = c.metrics(style.size)
        ink = c.ink(title)
        base = max(eyebrow.height + title_gap(style.size) + m.cap, eyebrow.height + 1 + ink.above)
        bottom = base + c.span(title) + max(m.desc, ink.below)
        progress = None
        if progress_label is not None:
            lm = c.metrics(progress_label.size_px)
            label_base = bottom + t.block_gap + lm.cap
            bar_y = label_base - (lm.cap + t.progress_h + 1) // 2
            bar_w = max(8, w - progress_label.width - t.gap)
            progress = (progress_label, label_base, bar_y, bar_w)
            bottom = max(bar_y + t.progress_h, label_base + max(lm.desc, c.ink(progress_label).below))
        body_base = bottom + t.block_gap + c.metrics(body_style.size).cap if body_style is not None else 0
        return cls(view, x, w, col_w, eyebrow, title, base, bottom, box, progress, body_style, body_base)


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True)
class Grid:
    """The list's slot grid: pitch, the title baseline's offset in a slot, the text start after the marker."""

    pitch: int
    base: int
    cap: int
    """Cap height of the rows' text: a slot's first ``base - cap`` rows are white."""
    text_x: int
    marker: int
    """Square marker size (XS), or 0: 16 px icons."""
    icon: int
    meta_base: int = 0
    """L rows: the meta line's baseline below the title's last baseline."""
    ink: int = 0
    """Where a Latin row's ink ends in its slot (the baseline plus the descender, or the meta line)."""

    @property
    def nudge(self) -> int:
        """How far a row may move down off the grid to keep a white row above its ink (else it takes a slot
        more)."""
        return max(2, self.pitch // 8)

    @classmethod
    def of(cls, c: Canvas) -> Grid:
        t = c.tokens
        m = c.metrics(c.size(t.row_title.size))
        icon = 0 if t.marker else c.icon_size(16)
        text_x = (t.marker or 16) + t.marker_gap
        if t.meta is None:
            pitch = max(t.row_pitch, m.cap + m.desc + 4)
            base = min(t.row_top + m.cap, pitch - m.desc - 1)
            return cls(pitch, base, m.cap, text_x, t.marker, icon, 0, base + m.desc)
        mm = c.metrics(c.size(t.meta.size))
        base = t.row_top + m.cap
        meta_base = m.desc + 4 + mm.cap
        pitch = max(t.row_pitch, base + meta_base + mm.desc + 5)
        return cls(pitch, base, m.cap, text_x, t.marker, icon, meta_base, base + meta_base + MINI_CHIP_PAD[0])

    def place(self, above: int, below: int, prev: int) -> tuple[int, int, int]:
        """(slots, baseline offset, nudge) of a row whose ink reaches ``above`` rows over its first baseline and
        ``below`` rows under it, after ink ending ``prev`` rows from the band's top (<= 0: in an earlier band).

        On the grid when its ink keeps one white row under ``prev`` (moving down at most `nudge` rows for that)
        and ends inside its band or in the white top rows of the next one (whose row checks it in turn); else
        centred in as many slots as the ink and a white row on each side need.
        """
        overhang = max(0, self.base - self.cap - 1)
        k = 1
        while True:
            if self.base + below <= k * self.pitch + overhang:
                nudge = max(0, prev + 1 - (self.base - above))
                if nudge <= self.nudge:
                    return k, self.base, nudge
            if k * self.pitch >= above + below + 2:
                base = (k * self.pitch - (above + below)) // 2 + above
                nudge = max(0, prev + 1 - (base - above))
                if nudge <= self.nudge:
                    return k, base, nudge
            k += 1


@dataclass
class Row:
    """One list row: a card's marker, title and time (or meta line), placed by `choose_rows`."""

    view: CardView
    title: TextBlock
    title_w: int
    time: TextBlock | None
    above: int
    """Ink rows over the first baseline (title and time)."""
    below: int
    """Ink rows under the first baseline (the title's other lines, descenders, the L meta line)."""
    label: TextBlock | None = None
    """L meta line: the card's label (in a mini chip for alert and caution cards)."""
    slots: int = 1
    base: int = 0
    """The title's first baseline from the band's top."""
    nudge: int = 0
    """Rows the band starts below the grid (`Grid.place`)."""

    @classmethod
    def measure(cls, c: Canvas, grid: Grid, view: CardView, w: int, now: datetime, settings: ScreenSettings,
                lines: int = 1) -> Row:
        t = c.tokens
        stamp = (progress_text(view, settings.language) if view.progress is not None
                 else card_stamp(view.ts, now, settings.language, settings.timezone))
        time = label = None
        if t.meta is not None:
            label = c.chrome(view.label, w, c.resolve(replace(t.chip, size=t.meta.size), view.label), align="left")
            time = c.chrome(stamp, w, c.resolve(t.meta, stamp), align="left")
            title_w = w - grid.text_x
        elif t.row_time is not None:
            time = c.chrome(stamp, w, c.resolve(t.row_time, stamp), align=c.ui_end)
            title_w = w - grid.text_x - time.width - t.gap
        else:
            title_w = w - grid.text_x
        style = c.resolve(replace(t.row_title, bold=view.tone == ALERT), view.title)
        title = c.text(view.title, max(1, title_w), style, language=view.language, lines=lines, align=c.ui_start)
        ink = c.ink(title)
        above = ink.above
        below = c.span(title) + ink.below
        if time is not None:
            tink = c.ink(time)
            if t.meta is None:
                above, below = max(above, tink.above), max(below, tink.below)
            else:
                below = c.span(title) + grid.meta_base + max(MINI_CHIP_PAD[0], tink.below)
        if grid.marker:  # the square sits on the cap height
            above = max(above, (c.metrics(style.size).cap + grid.marker + 1) // 2)
        return cls(view, title, max(1, title_w), time, above, below, label)

    def advance(self, grid: Grid) -> int:
        """Rows from this band's grid position to the next band's."""
        return self.nudge + self.slots * grid.pitch

    def cmds(self, c: Canvas, grid: Grid, x: int, w: int, y: int, *, ornaments: bool) -> Drawn:
        out = Drawn()
        base = y + self.nudge + self.base
        view = self.view
        m = c.metrics(self.title.size_px)
        if grid.marker:
            my = base - (m.cap + grid.marker + 1) // 2
            out.cmds += c.marker(x, my, grid.marker, c.tone_color(view.tone), filled=view.tone in (ALERT, CAUTION))
        elif grid.icon:
            out.cmds += c.icon(view.icon, grid.icon, x, base - grid.icon // 2 + (m.desc - m.cap) // 2,
                               c.tone_color(view.tone))
        out.text(c, self.title, x + grid.text_x, base, self.title_w)
        if self.label is None:
            if self.time is not None:
                out.text(c, self.time, x, base, w)
            return out
        # L meta line: [label or mini chip] [time]
        meta = base + c.span(self.title) + grid.meta_base
        lm = c.metrics(self.label.size_px)
        mx = x + grid.text_x
        end = mx + self.label.width
        if view.tone in (ALERT, CAUTION):
            pad_v, pad_h = MINI_CHIP_PAD
            chip_w, chip_h = self.label.width + 2 * pad_h, 2 * pad_v + lm.cap
            filled = view.tone == ALERT
            out.cmds += c.chip(mx, meta - lm.cap - pad_v, chip_w, chip_h, c.tone_color(view.tone), filled=filled,
                               notch=ornaments)
            out.text(c, self.label, mx + pad_h, meta, self.label.width, Color.WHITE if filled else Color.BLACK)
            end = mx + chip_w
        else:
            out.text(c, self.label, mx, meta, self.label.width)
        if self.time is not None and end + META_GAP + self.time.width <= x + w:
            out.text(c, self.time, end + META_GAP, meta, self.time.width)
        return out


@dataclass
class Listing:
    """The rows chosen for a column, and "+N MORE" after them when cards are left over."""

    rows: list[Row]
    more: TextBlock | None
    """"+N MORE" (None: nothing left over, or no room)."""
    more_nudge: int = 0
    ink_end: int = 0
    """Where the list's ink ends, from the first band's top (what the space under it is measured from)."""

    @property
    def drawn(self) -> bool:
        return bool(self.rows) or self.more is not None


def _shown(block: TextBlock) -> int:
    """Code points of ``block``'s text its lines show."""
    return block.lines[-1].end if block.lines else 0


def legible_row(block: TextBlock) -> bool:
    """Whether a row title says enough to count as shown: all of it, or `MIN_ROW_GLYPHS` characters."""
    if not block.truncated or not block.lines:
        return True
    shown = block.text[block.lines[0].start:block.lines[-1].end]
    return sum(not ch.isspace() for ch in shown) >= MIN_ROW_GLYPHS


def _ink_end(grid: Grid, row: Row) -> int:
    """Where ``row``'s ink ends relative to the next band's grid position (<= 0: inside its own band)."""
    return row.nudge + row.base + row.below - row.advance(grid)


def choose_rows(c: Canvas, grid: Grid, others: list[CardView], w: int, room: int, limit: int, now: datetime,
                settings: ScreenSettings, *, more_slot: bool = True) -> Listing:
    """The longest prefix of ``others`` whose ink ends within ``room`` rows of the first band's top (at most
    ``limit`` rows), keeping room for "+N MORE" while cards remain (unless ``more_slot`` is False: a footer
    counts them). The first band starts right under the hairline (its ink keeps a white row under it). In
    classes whose rows may take two lines, room still free lets cut titles wrap, in display order."""
    t = c.tokens

    def more_block(n: int) -> TextBlock:
        text = strings.more(n)
        return c.chrome(text, w, c.resolve(t.more, text), align="left")

    def place(row: Row, prev: int) -> Row:
        row.slots, row.base, row.nudge = grid.place(row.above, row.below, prev)
        return row

    def more_end(used: int, prev: int, pending: int) -> tuple[int, int]:
        """(ink end, nudge) of "+N MORE" in the band after ``used`` rows."""
        ink = c.ink(more_block(pending))
        nudge = max(0, prev + 1 - (grid.base - ink.above))
        return used + nudge + grid.base + ink.below, nudge

    def walk(rows: list[Row]) -> tuple[int, int, int]:
        """(used, prev, ink end) after placing ``rows`` in order."""
        used = prev = end = 0
        for row in rows:
            place(row, prev)
            end = used + row.nudge + row.base + row.below
            used += row.advance(grid)
            prev = _ink_end(grid, row)
        return used, prev, end

    rows: list[Row] = []
    used = prev = end = 0
    for i, view in enumerate(others):
        if len(rows) >= limit:
            break
        row = Row.measure(c, grid, view, w, now, settings)
        if not legible_row(row.title) and t.row_lines > 1 and row.title.lines[0].rtl == c.rtl:
            row = Row.measure(c, grid, view, w, now, settings, lines=t.row_lines)
        if not legible_row(row.title):
            break  # too little of the title would show: it waits under "+N MORE"
        row = place(row, prev)
        rest = len(others) - i - 1
        row_end = used + row.nudge + row.base + row.below
        after = (more_end(used + row.advance(grid), _ink_end(grid, row), rest)[0] if rest and more_slot
                 else row_end)
        if max(row_end, after) > room:
            break
        rows.append(row)
        used += row.advance(grid)
        prev, end = _ink_end(grid, row), row_end
    pending = len(others) - len(rows)
    keep_more = bool(pending and more_slot)
    if t.row_lines > 1 and rows:  # room left over lets cut titles take more lines, in display order
        wider_rows = list(rows)
        for i, row in enumerate(rows):
            if not (row.title.truncated and row.slots == 1) or row.title.lines[0].rtl != c.rtl:
                continue  # (a text against the UI's direction would wrap to the wrong side)
            wider = Row.measure(c, grid, row.view, w, now, settings, lines=t.row_lines)
            if _shown(wider.title) < _shown(row.title) + MIN_MORE_TEXT:
                continue  # a second line that shows next to nothing more is not worth a slot
            trial = wider_rows[:i] + [wider] + wider_rows[i + 1:]
            t_used, t_prev, t_end = walk(trial)
            if (more_end(t_used, t_prev, pending)[0] if keep_more else t_end) <= room:
                wider_rows = trial
        used, prev, end = walk(wider_rows)
        rows = wider_rows
    if not keep_more:
        return Listing(rows, None, 0, end)
    more_ink, nudge = more_end(used, prev, pending)
    if more_ink > room:
        return Listing(rows, None, 0, end)
    return Listing(rows, more_block(pending), nudge, more_ink)


def rows_cmds(c: Canvas, grid: Grid, listing: Listing, x: int, w: int, y: int, *, ornaments: bool) -> Drawn:
    """The rows from ``y`` down (separators between L rows are an ornament), then "+N MORE"."""
    out = Drawn()
    rows = listing.rows
    for i, row in enumerate(rows):
        out.extend(row.cmds(c, grid, x, w, y, ornaments=ornaments))
        y += row.advance(grid)
        if grid.meta_base and ornaments and (i < len(rows) - 1 or listing.more is not None):
            out.cmds += c.hairline(x + grid.text_x, y - 1, w - grid.text_x)
    if listing.more is not None:
        out.text(c, listing.more, x + grid.text_x, y + listing.more_nudge + grid.base, listing.more.width)
    return out


# --------------------------------------------------------------------------- empty state


def empty_cmds(c: Canvas, top: int, bottom: int) -> Drawn:
    """A check icon over "No updates", centred between ``top`` and ``bottom``."""
    t = c.tokens
    cw = t.content_width
    icon = c.icon_size(t.empty_icon)
    style = c.resolve(t.empty_text, strings.NO_UPDATES)
    text = c.chrome(strings.NO_UPDATES, cw, style, align="center")
    m = c.metrics(style.size)
    ink_top, ink_bottom = icon_ink(c, Icon.CHECK_CIRCLE, icon)
    gap = max(6, m.cap // 2)
    height = (ink_bottom - ink_top) + gap + m.cap + m.desc
    y = top + max(0, (bottom - top - height) // 2)
    out = Drawn()
    out.cmds += c.icon(Icon.CHECK_CIRCLE, icon, (cw - icon) // 2, y - ink_top, Color.BLACK)
    out.text(c, text, 0, y + (ink_bottom - ink_top) + gap + m.cap, cw)
    return out
