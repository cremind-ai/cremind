"""Screen composition: a tag's active cards -> one logical screen (docs/tags/layout.md "Screen model").

``compose_screen`` implements the `Composer` protocol of `compose.api` in the
"Editorial" design (`compose.tokens`, drawn by `compose.components`)::

    ┌────────────────────────────────────────────┐
    │ DESK                Updated Sun 2:05 PM    │  masthead: name in bold caps, when the screen was made
    │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │  accent rule (red on black/white/red panels)
    │ [?] ▐NEEDS YOU▌                   2:02 PM  │  eyebrow: icon, status chip or label, card time
    │ Approve deployment of           ┌──────┐   │  hero: the first card's title in bold, progress,
    │ release 2.4 to production?      │  QR  │   │  body (excerpts / pinned note), QR at the end side
    │ ────────────────────────────────└──────┘── │  hairline
    │ [!] Telegram channel stopped       Sep 26  │  rows: the next cards on a fixed pitch — marker or
    │ [▤] Reply ready: Tóm tắt cuộc họp 1:35 PM  │  icon, title, time ("7/12" for progress)
    │     +4 MORE                                │  overflow: cards counted, not shown
    └────────────────────────────────────────────┘

The logical canvas is the panel turned by ``panel.rotation``; its short side
picks the panel class (XS/S/M/L, `tokens.tokens_for`) and every size comes
from the class's tokens and the pack (`canvas.Canvas.resolve`), so 2.13" to
7.5" panels, both orientations, right-to-left UIs and packs without 12/14 px
or bold text all work. On L landscape panels the hero takes the start column
and the list the end column. One card alone is centred; no card shows "No
updates". XS panels on a pack without 12 px text have no masthead: a footer
row carries the time and "+N MORE".

Colour: black, plus the accent (`Canvas.accent`: RED on two-plane panels)
for the masthead rule, alert chips, alert row markers and icons, and the
identify frame — never text, never a 1 px line, never touching black. A
black/white screen is exactly the red one with RED drawn as BLACK.

The screen must stay within ``MAX_BYTES`` = ``min(LAYOUT_HARD_MAX,
LAYOUT_SERIAL_MAX)`` bytes (4000: DELIVER_LAYOUT's CBOR envelope has to fit one
serial frame), ``LAYOUT_MAX_GLYPHS`` glyphs and ``LAYOUT_MAX_COMMANDS``
commands, and within the render-cost bounds of §4.3 (``LAYOUT_MAX_QR`` QR
codes, ``LAYOUT_MAX_LINE_STEPS`` line steps): the composer draws at most one
QR code and no LINE (rules are RECTs), and its budget (`canvas.Limits`, read
from this module's globals on every call) refuses anything more. The body
gets whatever height and budget the rest leaves (fewer lines, down to none);
beyond that the composer walks the class's ladder (`plans_for`) — ornaments
off, fewer rows, no reserved body, a smaller title, then no masthead — and
takes the first plan that fits. Everything is a pure function of the inputs
(``now`` included), so the same cards give the same bytes.
"""

from __future__ import annotations

import dataclasses
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.tags.runtime.compose import strings
from app.tags.runtime.compose.api import ActiveCard, ComposedScreen, ScreenSettings, TagPanel
from app.tags.runtime.compose.canvas import LAYOUT_MARGIN, QR_QUIET, Budget, Canvas, Limits
from app.tags.runtime.compose.cards import CardView, ordered_views
from app.tags.runtime.compose.components import (
    Drawn,
    Footer,
    Grid,
    Hero,
    Listing,
    choose_rows,
    empty_cmds,
    header,
    rows_cmds,
)
from app.tags.runtime.compose.tokens import CAPS_12, CAPS_14, XS, TextRole, Tokens, tokens_for
from app.tags.runtime.fonts.fontset import FontSet
from app.tags.runtime.layout.engine import TextBlock
from app.tags.runtime.protocol.ids import (
    LAYOUT_HARD_MAX,
    LAYOUT_MAX_COMMANDS,
    LAYOUT_MAX_GLYPHS,
    LAYOUT_MAX_LINE_STEPS,
    LAYOUT_MAX_QR,
    LAYOUT_SERIAL_MAX,
    Color,
    QrEcc,
)
from app.tags.runtime.protocol.layout import (
    Layout,
    LayoutError,
    check_panel,
    check_strikes,
    encode_layout,
    qr_code,
)

MAX_BYTES = min(LAYOUT_HARD_MAX, LAYOUT_SERIAL_MAX)
"""Largest layout the companion produces: DELIVER_LAYOUT's CBOR envelope must fit one serial frame."""
BOTTOM = {"XS": 3, "S": 6, "M": 12, "L": 16}
"""White rows kept under the last ink of a screen, by class."""
_INKLESS = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf", "Mn", "Me"})


@dataclass(frozen=True)
class Plan:
    """One step of the degradation ladder (`plans_for`)."""

    rows: int
    """List rows at most (fewer when the height runs out)."""
    ornaments: bool = False
    """Chip corner notches, the eyebrow icon, separators between L rows."""
    body_min: int | None = None
    """Body lines kept before list rows get the height (None: the class's minimum)."""
    title_step: int = 0
    """Title ladder rungs skipped (largest first)."""
    title_lines: int | None = None
    """Lines of the last title rung at most."""
    header: str = "full"
    """``full`` (the time ladder), ``short`` (the bare time) or ``none``."""
    body: bool = True
    qr: bool = True


def plans_for(tokens: Tokens) -> tuple[Plan, ...]:
    """The degradation ladder of a panel class: the first plan is the full design, the last always fits."""
    most, floor, _target = tokens.rows
    smallest = len(tokens.titles) - 1
    plans = [Plan(most, ornaments=True), Plan(most)]
    plans += [Plan(rows) for rows in range(most - 1, floor - 1, -1)]
    plans += [Plan(rows, body_min=0) for rows in range(floor, -1, -1)]
    plans += [Plan(0, body_min=0, title_step=min(1, smallest), header="short"),
              Plan(0, body_min=0, title_step=smallest, title_lines=2, header="short"),
              Plan(0, body_min=0, title_step=smallest, title_lines=1, header="none", body=False, qr=False)]
    return tuple(plans)


def logical_size(panel: TagPanel) -> tuple[int, int]:
    """Logical canvas for the panel's rotation (§4.4 Rotation)."""
    return (panel.width, panel.height) if panel.rotation % 2 == 0 else (panel.height, panel.width)


def _limits() -> Limits:
    """The budget, read from this module's globals at call time (tests tighten them)."""
    return Limits(LAYOUT_MAX_GLYPHS, LAYOUT_MAX_COMMANDS, MAX_BYTES, LAYOUT_MAX_QR, LAYOUT_MAX_LINE_STEPS)


# --------------------------------------------------------------------------- screens


def _inked_prefix(text: str, count: int) -> int:
    """Length of the shortest prefix of ``text`` holding ``count`` characters that draw ink (not spaces, marks,
    controls or format characters): a budget of ``count - 1`` glyphs cannot show more of it."""
    for i, ch in enumerate(text):
        if unicodedata.category(ch) not in _INKLESS:
            count -= 1
            if count <= 0:
                return i + 1
    return len(text)


def _fit_body(c: Canvas, hero: Hero, limit: int, reserve: tuple[int, int, int], y: int) -> TextBlock | None:
    """The body with as many lines as end within ``limit`` rows of the hero's top and fit the budget left after
    ``reserve`` (the list, drawn later): laid out at the height's line count (only as much text as the glyphs
    left could draw), then cut to the budget."""
    if hero.body_style is None or not hero.view.body:
        return None
    glyphs_left = c.room()[0] - reserve[0]
    if glyphs_left <= 0:
        return None
    cut = _inked_prefix(hero.view.body, glyphs_left + 1)
    cut_binds = len(hero.view.body) > cut + LAYOUT_MARGIN
    lines = min(c.tokens.body_lines[1], hero.body_lines_for(c, limit))
    while lines >= 1:
        block = hero.body_block(c, lines, cut=cut)
        if cut_binds and not block.truncated:  # more than the glyphs left can draw ends in these lines
            lines = len(block.lines) - 1
            continue
        if hero.body_bottom(c, block) > limit:  # lines grown by their ink (stacked marks)
            lines = len(block.lines) - 1
            continue
        if c.fits(hero.body(c, block, y).cmds, reserve):
            return block
        lines = min(len(block.lines), _lines_within_budget(c, hero, block, reserve, y))
    return None


def _lines_within_budget(c: Canvas, hero: Hero, block: TextBlock, reserve: tuple[int, int, int], y: int) -> int:
    """How many of ``block``'s first lines fit the budget (one less than the first that does not, at least one
    less than ``block`` has: the cut line gains an ellipsis)."""
    for keep in range(len(block.lines) - 1, 0, -1):
        partial = TextBlock(block.text, block.size_px, block.box_width, block.lines[:keep],
                            tuple(g for g in block.glyphs if g.line < keep), block.height, True, block.unsupported,
                            0, 0)
        if c.fits(hero.body(c, partial, y).cmds, (reserve[0] + 1, reserve[1] + 1, reserve[2] + 8)):
            return keep
    return 0


def _area(c: Canvas, plan: Plan, now: datetime, settings: ScreenSettings) -> tuple[int, int, int, Footer | None]:
    """Draw the masthead; returns (row under the rule, content top, content bottom, footer)."""
    t = c.tokens
    if plan.header == "none":
        return 0, t.margin, t.height - BOTTOM[t.cls], None
    if t.cls == XS and 12 not in c.sizes:  # no 12 px text: the time goes to a footer row
        footer = Footer.measure(c)
        return 0, t.margin, footer.top - 2, footer
    rule_end = header(c, now, settings, plan.header)
    return rule_end, rule_end + t.after_rule, t.height - BOTTOM[t.cls], None


def _compose(panel: TagPanel, views: list[CardView], fonts: FontSet, settings: ScreenSettings, now: datetime,
             plan: Plan, tokens: Tokens, limits: Limits) -> tuple[Canvas, list[int], list[int]]:
    c = Canvas(panel, fonts, settings.language, tokens, limits)
    rule_end, top, bottom, footer = _area(c, plan, now, settings)
    if not views:
        out = empty_cmds(c, rule_end, footer.top if footer else tokens.height)
        if footer is not None:
            out.extend(footer.cmds(c, now, settings, 0))
        c.add(out.cmds, out.blocks)
        return c, [], []
    if tokens.split and len(views) > 1:
        return _split(c, views, top, bottom, now, settings, plan)
    return _stacked(c, views, rule_end, top, bottom, footer, now, settings, plan)


def _stacked(c: Canvas, views: list[CardView], rule_end: int, top: int, bottom: int, footer: Footer | None,
             now: datetime, settings: ScreenSettings, plan: Plan) -> tuple[Canvas, list[int], list[int]]:
    """Hero over the list (every class but L landscape)."""
    t = c.tokens
    cw = t.content_width
    first, others = views[0], views[1:]
    grid = Grid.of(c)
    more_slot = footer is None  # with a footer, "+N MORE" is in the footer
    target = min(t.rows[2], len(others), plan.rows)
    reserve_slots = target + (1 if more_slot and len(others) > target else 0)
    reserve_h = t.section_gap + 1 + (reserve_slots - 1) * grid.pitch + grid.ink if reserve_slots else 0
    body_min = t.body_lines[0] if plan.body_min is None else plan.body_min
    hero = Hero.measure(c, first, 0, cw, bottom - top - reserve_h, now, settings, ornaments=plan.ornaments,
                        title_step=plan.title_step, title_lines=plan.title_lines, body=plan.body,
                        body_reserve=body_min, qr=plan.qr)
    lift = max(0, -hero.eyebrow.top - (top - rule_end - 2 if rule_end else top - 1))
    y = top + lift
    reserved = hero.body_block(c, hero.body_reserve) if hero.body_style is not None and hero.body_reserve else None
    if y + hero.fixed_bottom > bottom and plan.header != "none":
        raise Budget(hero=True)  # not even the title fits under the masthead: the later plans drop it
    if not c.fits(hero.fixed(c, y).cmds):
        raise Budget(hero=True)

    rows_top = y + hero.bottom(c, reserved) + t.section_gap + 1  # under the hairline
    room = max(0, bottom - rows_top) if others else 0
    listing = choose_rows(c, grid, others, cw, room, plan.rows, now, settings, more_slot=more_slot)
    shown = [first.delivery_id] + [r.view.delivery_id for r in listing.rows]
    pending = [v.delivery_id for v in views[1 + len(listing.rows):]]

    # Budget: the hero's fixed parts, the list and the footer first; the body gets what they leave.
    tail = footer.cmds(c, now, settings, len(pending)) if footer is not None else Drawn()
    rows = rows_cmds(c, grid, listing, 0, cw, 0, ornaments=plan.ornaments)
    if listing.drawn:
        rows.cmds += c.hairline(0, 0, cw)
    committed = hero.fixed(c, y).cmds + rows.cmds + tail.cmds
    if not c.fits(committed):
        raise Budget(_most_rows(c, grid, listing, 0, cw, hero.fixed(c, y).cmds + tail.cmds, len(others),
                                more_slot, plan, hairline=True))
    limit = bottom - y - (listing.ink_end + t.section_gap + 1 if listing.drawn else 0)
    body = _fit_body(c, hero, limit, c.cost(committed), y) if plan.body else None

    if len(views) == 1:  # a lone card is centred in the white under the rule
        height = hero.bottom(c, body) - hero.eyebrow.top
        area_bottom = footer.top if footer else t.height
        y = max(y, rule_end + (area_bottom - rule_end - height) // 2 - hero.eyebrow.top)
    out = hero.fixed(c, y)
    if body is not None:
        out.extend(hero.body(c, body, y))
    if listing.drawn:
        line = y + hero.bottom(c, body) + t.section_gap
        out.cmds += c.hairline(0, line, cw)
        out.extend(rows_cmds(c, grid, listing, 0, cw, line + 1, ornaments=plan.ornaments))
    out.extend(tail)
    c.add(out.cmds, out.blocks)
    return c, shown, pending


def _split(c: Canvas, views: list[CardView], top: int, bottom: int, now: datetime, settings: ScreenSettings,
           plan: Plan) -> tuple[Canvas, list[int], list[int]]:
    """L landscape: the hero in the start column, the list in the end column, a 1 px divider between."""
    t = c.tokens
    cw = t.content_width
    hero_w = cw * 9 // 16
    divider = hero_w + t.margin
    list_x = divider + 1 + t.margin
    list_w = cw - list_x
    first, others = views[0], views[1:]
    body_min = t.body_lines[0] if plan.body_min is None else plan.body_min
    hero = Hero.measure(c, first, 0, hero_w, bottom - top, now, settings, ornaments=plan.ornaments,
                        title_step=plan.title_step, title_lines=plan.title_lines, body=plan.body,
                        body_reserve=body_min, qr=plan.qr)
    y = top + max(0, -hero.eyebrow.top - (t.after_rule - 2))
    fixed = hero.fixed(c, y)
    fixed.cmds += c.rect(divider, top, 1, bottom - top, Color.BLACK)
    if not c.fits(fixed.cmds):
        raise Budget(hero=True)
    grid = Grid.of(c)
    listing = choose_rows(c, grid, others, list_w, bottom - top, plan.rows, now, settings)
    shown = [first.delivery_id] + [r.view.delivery_id for r in listing.rows]
    pending = [v.delivery_id for v in views[1 + len(listing.rows):]]
    before = list(fixed.cmds)
    fixed.extend(rows_cmds(c, grid, listing, list_x, list_w, top, ornaments=plan.ornaments))
    if not c.fits(fixed.cmds):
        raise Budget(_most_rows(c, grid, listing, list_x, list_w, before, len(others), True, plan))
    body = _fit_body(c, hero, bottom - y, c.cost(fixed.cmds), y) if plan.body else None
    if body is not None:
        fixed.extend(hero.body(c, body, y))
    c.add(fixed.cmds, fixed.blocks)
    return c, shown, pending


def _most_rows(c: Canvas, grid: Grid, listing: Listing, x: int, w: int, fixed: list[Any], others: int,
               more_slot: bool, plan: Plan, *, hairline: bool = False) -> int | None:
    """The most of ``listing``'s rows that fit the budget beside ``fixed`` (with "+N MORE" for the rest), or
    None when that is not worth knowing (ornaments still on: the next plan drops them first)."""
    if plan.ornaments:
        return None
    base = c.cost(fixed)
    line = c.cost(c.hairline(0, 0, w)) if hairline else (0, 0, 0)
    per_row = [c.cost(row.cmds(c, grid, x, w, 0, ornaments=False).cmds) for row in listing.rows]

    def more(n: int) -> tuple[int, int, int]:
        if not (more_slot and n):
            return (0, 0, 0)
        text = strings.more(n)
        block = c.chrome(text, w, c.resolve(c.tokens.more, text), align="left")
        return c.cost(c.glyph_cmds(block, x + grid.text_x, 0, block.width, Color.BLACK))

    for j in range(len(listing.rows) - 1, -1, -1):
        parts = [base, *per_row[:j], more(others - j)]
        if j or (more_slot and others):
            parts.append(line)
        if c.fits_cost(tuple(sum(p[i] for p in parts) for i in range(3))):  # type: ignore[arg-type]
            return j
    return -1


def _cannot_fit(plan: Plan, failed: Plan, exc: Budget, tokens: Tokens, others: int) -> bool:
    """Whether ``plan``, later in the ladder, must fail the way ``failed`` did (`Budget`): with the same title,
    masthead, QR and ornaments when the hero did not fit; or, when the list did, differing only by allowing more
    rows than fit, with the same hero (the rows the title leaves room for unchanged) and rows that never wrap."""
    if exc.hero:
        keep = ("ornaments", "title_step", "title_lines", "header", "qr")
        return all(getattr(plan, k) == getattr(failed, k) for k in keep) and plan.rows <= failed.rows
    if exc.rows is None or tokens.row_lines > 1 or plan.rows <= exc.rows:
        return False
    if dataclasses.replace(plan, rows=failed.rows) != failed:
        return False
    target = tokens.rows[2]
    return tokens.split or min(target, others, plan.rows) == min(target, others, failed.rows)


def _finish(c: Canvas) -> bytes:
    layout = c.layout()
    data = encode_layout(layout)  # §4.3 structural checks
    if len(data) > MAX_BYTES:
        raise Budget
    check_strikes(layout, c.fonts.has_strike)
    check_panel(layout, c.panel.width, c.panel.height)
    return data


def compose_screen(panel: TagPanel, cards: list[ActiveCard], fonts: FontSet, settings: ScreenSettings,
                   now: datetime) -> ComposedScreen:
    """The `Composer`: masthead, hero card, list rows and "+N MORE" (module docstring)."""
    views = ordered_views(cards, settings)
    tokens = tokens_for(*logical_size(panel))
    limits = _limits()
    last_error: Exception | None = None
    failed: tuple[Plan, Budget] | None = None
    for plan in plans_for(tokens):
        if failed is not None and _cannot_fit(plan, *failed, tokens, len(views) - 1):
            continue  # it would fail the same way: no need to compose it
        try:
            c, shown, pending = _compose(panel, views, fonts, settings, now, plan, tokens, limits)
            data = _finish(c)
        except (Budget, LayoutError) as exc:
            last_error = exc
            failed = (plan, exc) if isinstance(exc, Budget) else None
            continue
        return ComposedScreen(data, tuple(shown), len(pending), tuple(c.unsupported), tuple(pending))
    raise RuntimeError(f"no plan fits the layout limits: {last_error!r}")


# --------------------------------------------------------------------------- identify, setup code, blank


def _label_role(tokens: Tokens) -> TextRole:
    return CAPS_14 if tokens.cls == "L" else CAPS_12


def compose_identify(panel: TagPanel, fonts: FontSet, tag_id: int | str | None = None) -> ComposedScreen:
    """"Which tag is this?": a frame in the accent colour, "CREMIND TAG", the tag id at the largest size up to
    32 px (bold where the pack has it) and the tag's name, centred.

    What the daemon delivers (as a new revision) for the ``identify`` command.
    ``tag_id`` defaults to ``panel.tag_id`` (an int prints as 8 hex digits).
    """
    tid = panel.tag_id if tag_id is None else tag_id
    id_text = f"{tid:08X}" if isinstance(tid, int) else str(tid)
    tokens = tokens_for(*logical_size(panel))
    limits = _limits()
    name_text = panel.name.strip()
    for name_lines in (2, 1, 0):
        c = Canvas(panel, fonts, "en", tokens, limits)
        cw = tokens.content_width
        label = c.chrome(strings.CREMIND_TAG, cw, c.resolve(_label_role(tokens), strings.CREMIND_TAG), align="center")
        big = c.chrome(id_text, cw, c.resolve(TextRole(32, bold=True), id_text, floor=False), align="center")
        name_size = 16 if tokens.cls in ("XS", "S") else 24
        name = (c.text(name_text, cw, c.resolve(TextRole(name_size), name_text), language="", align="center",
                       lines=name_lines) if name_text and name_lines else None)
        lm, im = c.metrics(label.size_px), c.metrics(big.size_px)
        gap = 8 if tokens.cls in ("XS", "S") else 14
        id_base = lm.cap + gap + max(im.cap, c.ink(big).above)
        bottom = id_base + max(im.desc, c.ink(big).below)
        name_base = 0
        if name is not None:
            nm = c.metrics(name.size_px)
            name_base = bottom + gap + max(nm.cap, c.ink(name).above)
            bottom = name_base + c.span(name) + max(nm.desc, c.ink(name).below)
        inner = c.height - 2 * (tokens.frame + 4)
        if bottom > inner and name_lines:
            continue
        y = (c.height - bottom) // 2
        out = Drawn(c.rect(-tokens.margin, 0, c.width, c.height, c.accent, tokens.frame))
        out.text(c, label, 0, y + lm.cap, cw)
        out.text(c, big, 0, y + id_base, cw)
        if name is not None:
            out.text(c, name, 0, y + name_base, cw)
        try:
            c.add(out.cmds, out.blocks)
            return ComposedScreen(_finish(c), (), 0, tuple(c.unsupported))
        except Budget:
            continue
    raise RuntimeError("the identify screen does not fit the layout limits")


def _code_lines(code: str) -> list[str]:
    """The grouped code two groups per line: "K3F9Z-2ZQMA" / "7H0W1-QDXPB" / "4TNC8"."""
    groups = code.split("-")
    return ["-".join(groups[i:i + 2]) for i in range(0, len(groups), 2)] or [code]


def compose_setup_code(panel: TagPanel, fonts: FontSet, code: str, qr_text: str) -> ComposedScreen:
    """A removed tag's last screen (docs/connect-setup.md §5.1 ``RELEASE``): the fresh setup code as a QR
    and in text, so the next owner can add the tag (the printed label no longer works).

    ``qr_text`` is the ``CTAG:`` QR text, ``code`` the grouped code a person types. Landscape panels put the
    QR at the start side and "ADD THIS TAG", the code (two groups per line) and where to type it beside it;
    portrait panels put the QR on top. When that does not fit, the hint loses lines, then the label goes,
    then the code gets smaller, then the QR's modules.
    """
    try:
        modules = qr_code(qr_text.encode("ascii"), int(QrEcc.LOW)).get_size()
    except LayoutError as exc:
        raise ValueError(f"the setup code does not fit a QR code: {exc}") from None
    tokens = tokens_for(*logical_size(panel))
    limits = _limits()
    w, h, m = tokens.width, tokens.height, tokens.margin
    landscape = w >= h
    box_room = (h - 2 * m, w // 2) if landscape else (w - 2 * m, h // 2)
    qr_modules = [p for p in (6, 5, 4, 3, 2, 1) if (modules + 2 * QR_QUIET) * p <= min(box_room)] or [1]
    lines = _code_lines(code)
    for module in qr_modules:
        box = (modules + 2 * QR_QUIET) * module
        probe = Canvas(panel, fonts, "en", tokens, limits)
        sizes = sorted({probe.size(s) for s in probe.sizes if s <= tokens.code.size} | {probe.size(12)},
                       reverse=True)
        for size in sizes:
            for with_label in (True, False):
                for hint_lines in (2, 1, 0):
                    c = Canvas(panel, fonts, "en", tokens, limits)
                    screen = _setup_layout(c, landscape, box, module, modules, lines, size, with_label, hint_lines,
                                           qr_text)
                    if screen is not None:
                        return screen
    raise RuntimeError("the setup-code screen does not fit this panel")


def _setup_layout(c: Canvas, landscape: bool, box: int, module: int, modules: int, lines: list[str], size: int,
                  with_label: bool, hint_lines: int, qr_text: str) -> ComposedScreen | None:
    """One try of the setup-code screen; None when it does not fit."""
    t = c.tokens
    cw = t.content_width
    col_x, col_w = (box + t.gap, cw - box - t.gap) if landscape else (0, cw)
    align = c.ui_start if landscape else "center"
    blocks: list[tuple[TextBlock, int]] = []  # (block, gap above its capitals)
    if with_label:
        label = c.chrome(strings.ADD_THIS_TAG, col_w, c.resolve(_label_role(t), strings.ADD_THIS_TAG), align=align)
        if label.truncated:
            return None
        blocks.append((label, 0))
    code_style = c.resolve(TextRole(size, bold=True), "".join(lines), floor=False)
    for i, text in enumerate(lines):
        block = c.chrome(text, col_w, code_style, align=align)
        if block.truncated or block.width > col_w:
            return None
        blocks.append((block, (8 if blocks else 0) if i == 0 else 4))
    if hint_lines:
        hint = c.text(strings.ADD_TAG_HINT, col_w, c.resolve(TextRole(12), strings.ADD_TAG_HINT), language="en",
                      align=align, lines=hint_lines, direction="ltr")
        if hint.truncated:
            return None
        blocks.append((hint, 8))
    # The text column: baselines on the cap height, every gap measured from the previous block's descender.
    rel: list[int] = []
    y = 0
    for block, gap in blocks:
        bm = c.metrics(block.size_px)
        base = y + gap + max(bm.cap, c.ink(block).above)
        rel.append(base)
        y = base + c.span(block) + max(bm.desc, c.ink(block).below)
    text_h = y
    if landscape:
        if text_h > c.height - 2 * t.margin:
            return None
        qr_y = (c.height - box) // 2
        text_y = (c.height - text_h) // 2
    else:
        if box + t.gap + text_h > c.height - 2 * t.margin:
            return None
        qr_y = (c.height - box - t.gap - text_h) // 2
        text_y = qr_y + box + t.gap
    quiet = QR_QUIET * module
    side = modules * module
    out = Drawn()
    qr_x = 0 if landscape else (cw - box) // 2
    out.cmds += c.qr(qr_x + quiet, qr_y + quiet, side, module, qr_text.encode("ascii"))
    for (block, _gap), base in zip(blocks, rel, strict=True):
        out.text(c, block, col_x, text_y + base, col_w)
    try:
        c.add(out.cmds, out.blocks)
        return ComposedScreen(_finish(c), (), 0, tuple(c.unsupported))
    except Budget:
        return None


def compose_blank(panel: TagPanel) -> ComposedScreen:
    """An all-white screen (``clear`` jobs, ownership changes)."""
    w, h = logical_size(panel)
    layout = Layout(w, h, panel.rotation, Color.WHITE, ())
    data = encode_layout(layout)
    check_panel(layout, panel.width, panel.height)
    return ComposedScreen(data, (), 0)
