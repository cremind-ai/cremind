"""One screen under construction: the §4.3 budget, resolved text styles and mirrored shapes.

`Canvas` holds the commands of a screen being composed and refuses any that
would break a limit (`Budget`, the composer then tries its next plan). The
limits arrive as a `Limits` value that ``compose.screen`` builds from its own
module globals at call time, so this module never imports the screen module
(tests tighten ``screen.LAYOUT_MAX_*`` / ``screen.MAX_BYTES``).

Coordinates are **start-relative**: components measure x from the start side
of the UI language and `Canvas.x` mirrors them in right-to-left UIs; every
shape helper (`rect`, `chip`, `marker`, `progress`, `qr`, `icon`) mirrors and
clamps (RECT and PROGRESS sizes are u16, a negative size would not encode).

Text: `resolve` turns a `tokens.TextRole` into a `Style` the pack can draw —
the size (largest available <= wanted, else the smallest; then raised to the
text's legibility floor), bold only when the pack has Noto Sans Bold at that
size and the text has no stacked diacritics below 14 px (they merge in the
12 px bold strike), letter-spacing only for Latin, Greek and Cyrillic.
`metrics` measures cap height and descender on the base face's ink; `text`
lays out with tight leading on the `tokens.PITCH` grid; `ink` gives a
block's real ink above its first and below its last baseline, which every
vertical gap is checked against (stacked Vietnamese marks, Arabic, CJK).

Colour: BLACK everywhere except the accent (`accent`: RED on two-plane
panels, BLACK on one-plane panels — the composer emits the colour itself, so
a black/white screen is exactly the red one with RED drawn as BLACK).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

import icu

from app.tags.runtime.compose.api import TagPanel
from app.tags.runtime.compose.tokens import TextRole, Tokens, pitch
from app.tags.runtime.fonts.fontset import FontSet
from app.tags.runtime.layout.engine import MAX_WIDTH, TextBlock, layout_text
from app.tags.runtime.layout.fonts import FontContext
from app.tags.runtime.layout.legibility import legible_size
from app.tags.runtime.layout.unicode import icu_locale, is_rtl_language, script_of
from app.tags.runtime.protocol.ids import ICON_SIZES, Color, QrEcc
from app.tags.runtime.protocol.layout import Command, Glyphs, Layout, Line, Progress, Qr, Rect, line_steps
from app.tags.runtime.protocol.layout import Icon as IconCmd
from app.tags.runtime.protocol.msgs import (
    LayoutCmdGlyphs,
    LayoutCmdIcon,
    LayoutCmdLine,
    LayoutCmdProgress,
    LayoutCmdQr,
    LayoutCmdRect,
    LayoutGlyph,
    LayoutHeader,
)

QR_QUIET = 2
"""Quiet-zone modules kept white around a QR code's ink."""
LAYOUT_MARGIN = 32
"""Code points laid out beyond what a text's lines can show (`Canvas.text`): line breaking, shaping and bidi
look that far ahead at most, so the shown lines come out the same as from the whole text."""
_TRACKED_SCRIPTS = frozenset({"Latn", "Grek", "Cyrl"})
_NEUTRAL_SCRIPTS = frozenset({"Zyyy", "Zinh", "Zzzz"})
_I16 = (-0x8000, 0x7FFF)


@dataclass(frozen=True)
class Limits:
    """The §4.3 budget of one screen (``compose.screen`` builds it from its module globals per call)."""

    glyphs: int
    commands: int
    bytes: int
    qr: int
    line_steps: int


class Budget(Exception):
    """A part does not fit the limits (or the canvas): the composer tries its next plan.

    ``rows``: when the list made it overflow, the most rows the same plan could have drawn instead (plans that
    differ only by allowing more rows than that cannot fit either, and are skipped). ``hero``: the masthead and
    the hero's fixed parts alone did not fit (plans that keep the same title, masthead, QR and ornaments
    cannot fit either: with fewer rows the hero only gets room to grow).
    """

    def __init__(self, rows: int | None = None, *, hero: bool = False) -> None:
        super().__init__(rows, hero)
        self.rows = rows
        self.hero = hero


@dataclass(frozen=True)
class Style:
    """A resolved text role: what `layout_text` is called with."""

    size: int
    weight: str = "regular"
    tracking: int = 0

    @property
    def bold(self) -> bool:
        return self.weight == "bold"


@dataclass(frozen=True)
class Metrics:
    """One text size measured on the base face's ink."""

    cap: int
    """Ink rows of "H" above the baseline."""
    desc: int
    """Ink rows of "p" below the baseline."""
    pitch: int


@dataclass(frozen=True)
class Ink:
    """A text block's real ink: rows above its first baseline and below its last."""

    above: int
    below: int


def stacked_marks(text: str) -> bool:
    """Whether a base character of ``text`` carries two or more combining marks (Vietnamese "ắ", "ệ")."""
    run = 0
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch):
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False


def tracked(text: str) -> bool:
    """Whether letter-spacing applies: ``text`` has letters, all Latin, Greek or Cyrillic."""
    scripts = {script_of(ord(ch)) for ch in text} - _NEUTRAL_SCRIPTS
    return bool(scripts) and scripts <= _TRACKED_SCRIPTS


def _cost(cmd: Command) -> int:
    """Encoded bytes of one command."""
    if isinstance(cmd, Glyphs):
        return 1 + LayoutCmdGlyphs.LEN + LayoutGlyph.LEN * len(cmd.glyphs)
    if isinstance(cmd, Qr):
        return 1 + LayoutCmdQr.LEN + len(cmd.text)
    return 1 + {IconCmd: LayoutCmdIcon, Line: LayoutCmdLine, Progress: LayoutCmdProgress}.get(
        type(cmd), LayoutCmdRect).LEN


def _i16(v: int) -> int:
    return max(_I16[0], min(_I16[1], v))


def _u16(v: int) -> int:
    return max(0, min(0xFFFF, v))


class Canvas:
    """Commands of one screen under construction (see the module docstring)."""

    def __init__(self, panel: TagPanel, fonts: FontSet, language: str, tokens: Tokens, limits: Limits) -> None:
        self.panel = panel
        self.fonts = fonts
        self.ctx = FontContext.for_fontset(fonts)
        self.tokens = tokens
        self.limits = limits
        self.width, self.height = tokens.width, tokens.height
        self.language = language
        self.rtl = is_rtl_language(language)
        self.red = panel.planes == 2
        self.sizes = self.ctx.text_sizes()
        if not self.sizes:
            raise ValueError("the font pack has no text strikes")
        self.commands: list[Command] = []
        self.glyphs = 0
        self.bytes = LayoutHeader.LEN
        self.qrs = 0
        self.line_steps = 0
        self.uses_red = False
        self.unsupported: dict[str, None] = {}
        self._metrics: dict[int, Metrics] = {}

    # ------------------------------------------------------------------ budget

    def room(self) -> tuple[int, int, int]:
        """Glyphs, commands and bytes still available."""
        return (self.limits.glyphs - self.glyphs, self.limits.commands - len(self.commands),
                self.limits.bytes - self.bytes)

    @staticmethod
    def cost(cmds: Sequence[Command]) -> tuple[int, int, int]:
        """Glyphs, commands and bytes of ``cmds``."""
        return (sum(len(c.glyphs) for c in cmds if isinstance(c, Glyphs)), len(cmds), sum(_cost(c) for c in cmds))

    def fits(self, cmds: Sequence[Command], reserve: tuple[int, int, int] = (0, 0, 0)) -> bool:
        """Whether ``cmds`` fit what is left, after ``reserve`` (glyphs, commands, bytes) for later parts."""
        g, n, b = self.cost(cmds)
        qrs = sum(isinstance(c, Qr) for c in cmds)
        steps = sum(line_steps(c) for c in cmds if isinstance(c, Line))
        return (self.fits_cost((g + reserve[0], n + reserve[1], b + reserve[2]))
                and self.qrs + qrs <= self.limits.qr and self.line_steps + steps <= self.limits.line_steps)

    def fits_cost(self, cost: tuple[int, int, int]) -> bool:
        """Whether ``cost`` (glyphs, commands, bytes) fits what is left."""
        rg, rn, rb = self.room()
        return cost[0] <= rg and cost[1] <= rn and cost[2] <= rb

    def add(self, cmds: Sequence[Command], blocks: Sequence[TextBlock] = ()) -> None:
        """Append ``cmds`` (raises `Budget` when they do not fit); ``blocks`` are the texts they draw."""
        if not self.fits(cmds):
            raise Budget
        g, n, b = self.cost(cmds)
        self.glyphs += g
        self.bytes += b
        self.qrs += sum(isinstance(c, Qr) for c in cmds)
        self.line_steps += sum(line_steps(c) for c in cmds if isinstance(c, Line))
        self.commands.extend(cmds)
        if any(getattr(c, "color", Color.BLACK) == Color.RED for c in cmds):
            self.uses_red = True
        for block in blocks:
            for ch in block.unsupported:
                self.unsupported.setdefault(ch, None)

    def layout(self) -> Layout:
        return Layout(self.width, self.height, self.panel.rotation, Color.WHITE, tuple(self.commands),
                      flags=1 if self.uses_red else 0)

    # ------------------------------------------------------------------ geometry and colour

    def x(self, x: int, w: int) -> int:
        """Left edge of a ``w``-wide element placed ``x`` from the start-side margin (mirrored in RTL UIs)."""
        x += self.tokens.margin
        return self.width - x - w if self.rtl else x

    @property
    def ui_start(self) -> str:
        return "right" if self.rtl else "left"

    @property
    def ui_end(self) -> str:
        return "left" if self.rtl else "right"

    @property
    def accent(self) -> int:
        """The one colour beside black: RED on two-plane panels."""
        return Color.RED if self.red else Color.BLACK

    def tone_color(self, tone: str) -> int:
        """Markers, row icons and chips: the accent for ``alert``, else black."""
        return self.accent if tone == "alert" else Color.BLACK

    # ------------------------------------------------------------------ text

    def size(self, want: int, floor: int = 0) -> int:
        """A text size the pack has: the largest <= ``want`` (else the smallest), raised to ``floor`` (the
        smallest size >= it, else the largest)."""
        size = self.ctx.nearest_size(want)
        if floor > size:
            bigger = [s for s in self.sizes if s >= floor]
            size = min(bigger) if bigger else max(self.sizes)
        return size

    def resolve(self, role: TextRole, text: str = "", *, floor: bool = True, keep_bold: bool = False) -> Style:
        """The style ``text`` is drawn with for ``role`` (module docstring).

        ``keep_bold``: when only stacked marks keep a bold role regular below 14 px, take 14 px bold instead
        (the masthead's name stays bold).
        """
        size = self.size(role.size, legible_size(text) if floor and text else 0)
        bold = role.bold and self.ctx.has_weight("bold", size)
        if bold and size < 14 and stacked_marks(text):
            bigger = self.size(14, 14)
            bold = keep_bold and bigger >= 14 and self.ctx.has_weight("bold", bigger)
            size = bigger if bold else size
        tracking = role.tracking if role.tracking and tracked(text) else 0
        return Style(size, "bold" if bold else "regular", tracking)

    def caps(self, text: str, language: str | None = None) -> str:
        """``text`` uppercased by ICU in ``language`` (the UI language by default): "i" -> "İ" in Turkish."""
        return str(icu.UnicodeString(text).toUpper(icu_locale(language if language is not None else self.language)))

    def metrics(self, size: int) -> Metrics:
        m = self._metrics.get(size)
        if m is None:
            base = self.ctx.base_face(size)
            font = self.ctx.hb_font(base, size)

            def ink(ch: str) -> tuple[int, int, int, int] | None:
                gid = font.get_nominal_glyph(ord(ch))
                return self.ctx.ink(base, size, gid) if gid else None

            h, p = ink("H"), ink("p")
            cap = -h[1] if h else (size * 3 + 2) // 4
            desc = p[3] if p else (size + 2) // 4
            m = self._metrics[size] = Metrics(cap, desc, pitch(size))
        return m

    def text(self, text: str, width: int, style: Style, *, language: str | None = None, lines: int = 1,
             align: str = "start", direction: str = "auto", cut: int | None = None) -> TextBlock:
        """``text`` laid out in a ``width``-wide box, at most ``lines`` lines on the `tokens.PITCH` grid.

        The engine analyses a whole paragraph whatever ``lines`` is, so only a prefix that surely covers the
        lines is laid out: ``lines`` x ``width / 2`` code points (no real text averages under 2 px per code
        point) plus a margin; when that prefix turns out to fit whole, the full text is laid out after all, so
        the cut never hides an ellipsis. ``cut`` (code points, when smaller) is the caller's own bound: past it
        the text cannot be drawn anyway, and a prefix that fits whole comes back as it is (`_fit_body` knows).
        """
        ascent, descent = self.ctx.line_box(style.size, "tight")
        width = max(1, min(width, MAX_WIDTH))

        def lay(t: str) -> TextBlock:
            return layout_text(t, self.fonts, width=width, size_px=style.size,
                               language=language if language is not None else self.language,
                               direction=direction, align=align, max_lines=max(1, lines),  # type: ignore[arg-type]
                               line_spacing=self.metrics(style.size).pitch - ascent - descent,
                               weight=style.weight, leading="tight", tracking=style.tracking)  # type: ignore[arg-type]

        bound = max(1, lines) * max(16, width // 2) + LAYOUT_MARGIN
        if cut is not None and cut + LAYOUT_MARGIN < min(bound, len(text)):
            return lay(text[:cut + LAYOUT_MARGIN])
        if len(text) <= bound:
            return lay(text)
        block = lay(text[:bound])
        return block if block.truncated else lay(text)

    def chrome(self, text: str, width: int, style: Style, *, align: str | None = None) -> TextBlock:
        """A composer string (English words around localised values): one LTR line, UI-side aligned."""
        return self.text(text, width, style, language=self.language, align=align or self.ui_start, direction="ltr")

    def ink(self, block: TextBlock) -> Ink:
        """Rows of real ink above the block's first baseline and below its last (0, 0 without ink)."""
        top = bottom = None
        for g in block.glyphs:
            box = self.ctx.ink(g.face_id, g.size_px, g.glyph_id)
            if box is None:
                continue
            t, b = g.y + box[1], g.y + box[3]
            top = t if top is None or t < top else top
            bottom = b if bottom is None or b > bottom else bottom
        if top is None or bottom is None or not block.lines:
            return Ink(0, 0)
        return Ink(block.lines[0].baseline - top, bottom - block.lines[-1].baseline)

    def span(self, block: TextBlock) -> int:
        """Distance from the block's first baseline to its last."""
        return block.lines[-1].baseline - block.lines[0].baseline if block.lines else 0

    def glyph_cmds(self, block: TextBlock, x: int, baseline: int, w: int, color: int) -> list[Command]:
        """GLYPHS drawing ``block`` (laid out ``w`` wide) at start offset ``x``, first baseline at ``baseline``."""
        if not block.lines:
            return []
        return list(block.commands(self.x(x, w), baseline - block.lines[0].baseline, color))

    # ------------------------------------------------------------------ shapes (start-relative, clamped)

    def rect(self, x: int, y: int, w: int, h: int, color: int, border: int = 0) -> list[Command]:
        w, h = _u16(w), _u16(h)
        if not w or not h:
            return []
        return [Rect(_i16(self.x(x, w)), _i16(y), w, h, max(0, min(255, border)), color)]

    def hairline(self, x: int, y: int, w: int) -> list[Command]:
        return self.rect(x, y, w, 1, Color.BLACK)

    def chip(self, x: int, y: int, w: int, h: int, color: int, *, filled: bool, notch: bool) -> list[Command]:
        """A label box: filled, or a 1 px outline; ``notch`` drops each corner pixel (reads as rounded)."""
        if w < 3 or h < 3 or not notch:
            return self.rect(x, y, w, h, color, 0 if filled else 1)
        if filled:
            return self.rect(x + 1, y, w - 2, h, color) + self.rect(x, y + 1, w, h - 2, color)
        return (self.rect(x + 1, y, w - 2, 1, color) + self.rect(x + 1, y + h - 1, w - 2, 1, color)
                + self.rect(x, y + 1, 1, h - 2, color) + self.rect(x + w - 1, y + 1, 1, h - 2, color))

    def marker(self, x: int, y: int, size: int, color: int, *, filled: bool) -> list[Command]:
        return self.rect(x, y, size, size, color, 0 if filled else 1)

    def progress(self, x: int, y: int, w: int, h: int, value: int, maximum: int) -> list[Command]:
        """A progress bar filling from the start side (PROGRESS fills left to right: RTL UIs get the same
        frame and fill drawn as two RECTs, the fill growing from the right)."""
        w, h = _u16(w), _u16(h)
        if not w or not h:
            return []
        scale = max(1, -(-maximum // 0xFFFF))
        value, maximum = min(value, maximum) // scale, maximum // scale
        if not self.rtl:
            return [Progress(_i16(self.x(x, w)), _i16(y), w, h, value, maximum, Color.BLACK)]
        left = self.x(x, w)
        fill = (w - 4) * value // maximum if maximum and w > 4 and h > 4 else 0
        out = [Rect(_i16(left), _i16(y), w, h, 1, Color.BLACK)]
        if fill:
            out.append(Rect(_i16(left + w - 2 - fill), _i16(y + 2), fill, h - 4, 0, Color.BLACK))
        return out

    def qr(self, x: int, y: int, side: int, module: int, text: bytes) -> list[Command]:
        """A QR code whose ink (``side`` px square) has its start-side top corner at (``x``, ``y``)."""
        return [Qr(_i16(self.x(x, side)), _i16(y), module, int(QrEcc.LOW), Color.BLACK, text)]

    def icon_size(self, want: int) -> int:
        """The largest icon strike <= ``want`` the pack has (0: none)."""
        have = [s for s in ICON_SIZES if s <= want and self.fonts.has_strike(0, s)]
        return max(have) if have else 0

    def icon(self, icon: int, size: int, x: int, y: int, color: int) -> list[Command]:
        if not size:
            return []
        return [IconCmd(int(icon), size, color, _i16(self.x(x, size)), _i16(y))]
