"""The design's rules, checked on composed screens: colour, contrast, collisions, density, legibility, fallbacks.

Every check runs over the gallery card sets (`design_checks.card_sets`: every tone, kind and script a screen
meets) on each panel class — XS landscape (the Hema) and portrait, S, M both ways, L — in English, with
excerpts and QR links, and in an Arabic (right-to-left) UI, on whichever pack the suite runs with
(``CREMIND_TAG_TEST_PACK`` selects an older one) and on the dev pack (16/24 px, no bold: the old shape).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.tags.runtime.compose import screen as screen_module
from app.tags.runtime.compose import strings
from app.tags.runtime.compose.api import ScreenSettings, TagPanel
from app.tags.runtime.compose.canvas import QR_QUIET, Canvas
from app.tags.runtime.compose.cards import ordered_views
from app.tags.runtime.compose.components import Grid, choose_rows, legible_row
from app.tags.runtime.compose.samples import example_cards
from app.tags.runtime.compose.screen import compose_identify, compose_screen, compose_setup_code, logical_size
from app.tags.runtime.compose.tokens import CAPS_12, L, M, S, XS, tokens_for
from app.tags.runtime.layout import FontContext
from app.tags.runtime.layout.legibility import legible_size
from app.tags.runtime.protocol.ids import Color, NodeRole
from app.tags.runtime.protocol.layout import Glyphs, Icon, Progress, Qr, Rect, decode_layout
from app.tags.runtime.secure.codes import SetupPayload

from .design_checks import (
    HEMA,
    HEMA_BW,
    HEMA_PORTRAIT,
    LANDSCAPE_BWR,
    LARGE,
    LONG,
    ORDER,
    PORTRAIT_BWR,
    SMALL_S,
    background,
    bw_twin,
    card,
    card_sets,
    check,
    colours,
    density,
    ink,
    with_red_as_black,
)

pytestmark = pytest.mark.fonts

PANELS = [HEMA, HEMA_PORTRAIT, SMALL_S, LANDSCAPE_BWR, PORTRAIT_BWR, LARGE]
"""One black/white/red panel per class and orientation (each test derives the black/white twin)."""
SETTINGS = {
    "en": ScreenSettings(False, False, "Asia/Ho_Chi_Minh", "en"),
    "en-excerpts-qr": ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "en"),
    "ar": ScreenSettings(True, True, "Asia/Riyadh", "ar"),
}
WHITE, BLACK, RED = Color.WHITE, Color.BLACK, Color.RED


def panel_id(p: TagPanel) -> str:
    w, h = logical_size(p)
    return f"{w}x{h}"


def screens(panel: TagPanel, fonts: Any, settings: ScreenSettings, now: datetime) -> list[tuple[str, Any]]:
    """(name, ComposedScreen) for every card set, identify and the setup code."""
    out = [(name, compose_screen(panel, cards, fonts, settings, now)) for name, cards in card_sets(now).items()]
    payload = SetupPayload(NodeRole.TAG, panel.tag_id, bytes(range(10)))
    out.append(("identify", compose_identify(panel, fonts)))
    out.append(("setup-code", compose_setup_code(panel, fonts, payload.code(), payload.qr_text())))
    return out


def has_bold(fonts: Any, size: int) -> bool:
    return FontContext.for_fontset(fonts).has_weight("bold", size)


# --------------------------------------------------------------------------- tokens


@pytest.mark.parametrize(("size", "cls", "portrait", "split"), [
    ((250, 128), XS, False, False), ((128, 250), XS, True, False), ((296, 128), XS, False, False),
    ((264, 176), S, False, False), ((200, 200), S, False, False), ((400, 300), M, False, False),
    ((300, 400), M, True, False), ((800, 480), L, False, True), ((480, 800), L, True, False),
    ((2048, 2048), L, False, False)])
def test_panel_classes(size: tuple[int, int], cls: str, portrait: bool, split: bool) -> None:
    t = tokens_for(*size)
    assert (t.cls, t.portrait, t.split) == (cls, portrait, split)
    assert t.rows[0] >= t.rows[2] >= t.rows[1] >= 1 and t.titles[0].whole and not t.titles[-1].whole
    assert t.name.bold and t.name.caps and t.name.tracking == 1 and t.margin >= 2 * min(t.qr_modules)


# --------------------------------------------------------------------------- colour


@pytest.mark.parametrize("lang", sorted(SETTINGS))
@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_bw_is_bwr_with_red_as_black(fonts: Any, now: datetime, panel: TagPanel, lang: str) -> None:
    """A black/white panel gets exactly the red screen with RED drawn as BLACK (only the uses-red flag
    differs): every red signal has a shape twin, so nothing is lost without red."""
    bw = bw_twin(panel)
    for (name, red), (_, black) in zip(screens(panel, fonts, SETTINGS[lang], now),
                                        screens(bw, fonts, SETTINGS[lang], now), strict=True):
        a, b = decode_layout(red.layout), decode_layout(black.layout)
        assert b.commands == with_red_as_black(a), name
        assert a.flags & ~1 == b.flags and not b.flags & 1, name
        assert (red.delivery_ids, red.pending_delivery_ids) == (black.delivery_ids, black.pending_delivery_ids)


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_text_keeps_contrast_on_both_panels(fonts: Any, now: datetime, panel: TagPanel, lang: str) -> None:
    """Black and red text and icons only over white; white text only over a black or red fill (a knock-out);
    and red never touches black (a white pixel at least between them, diagonals included)."""
    for p in (panel, bw_twin(panel)):
        for name, screen in screens(p, fonts, SETTINGS[lang], now):
            layout = decode_layout(screen.layout)
            w, h, under = colours(background(layout), fonts, p)
            for cmd in layout.commands:
                if not isinstance(cmd, Glyphs | Icon):
                    continue
                seen = {under[y * w + x] for x, y in ink(cmd, fonts) if 0 <= x < w and 0 <= y < h}
                if cmd.color == WHITE:
                    assert seen and seen <= {BLACK, RED}, (name, cmd)
                else:
                    assert seen <= {WHITE}, (name, cmd)
            if p.planes == 2:
                w, h, px = colours(layout, fonts, p)
                for i in (i for i, v in enumerate(px) if v == RED):
                    x, y = i % w, i // w
                    near = {px[yy * w + xx] for xx in (x - 1, x, x + 1) for yy in (y - 1, y, y + 1)
                            if 0 <= xx < w and 0 <= yy < h}
                    assert BLACK not in near, (name, x, y)


@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_knockout_text_sits_inside_its_fill(fonts: Any, now: datetime, panel: TagPanel) -> None:
    """White text is drawn inside an earlier solid RECT with at least a pixel of padding."""
    for lang in ("en", "ar"):
        for name, screen in screens(panel, fonts, SETTINGS[lang], now):
            commands = decode_layout(screen.layout).commands
            for i, cmd in enumerate(commands):
                if not (isinstance(cmd, Glyphs | Icon) and cmd.color == WHITE):
                    continue
                pixels = ink(cmd, fonts)
                fills = [r for r in commands[:i] if isinstance(r, Rect) and r.border == 0]
                assert pixels and any(all(r.x + 1 <= x < r.x + r.w - 1 and r.y + 1 <= y < r.y + r.h - 1
                                          for x, y in pixels) for r in fills), (name, cmd)


@pytest.mark.parametrize("panel", [*PANELS, HEMA_BW], ids=panel_id)
def test_urgent_cards_keep_a_non_colour_cue(fonts: Any, now: datetime, panel: TagPanel) -> None:
    """On a black/white panel an alert (needs_input, error) still differs from a calm card by shape: a filled
    chip over the hero title, and a filled square (XS) or a bold title (S/M/L, where the pack has bold) in the
    list — and the calm card has none of these."""
    bw = bw_twin(panel)
    tokens = tokens_for(*logical_size(panel))

    def screen(severity: str, kind: str = "notification") -> Any:
        cards = [card(1, kind, "Telegram channel stopped", 90, 1, now, severity=severity, icon="info"),
                 card(2, "notification", "Weekly summary", 40, 2, now)]
        rows = [card(1, "notification", "Weekly summary", 90, 1, now),
                card(2, kind, "Telegram channel stopped", 40, 2, now, severity=severity, icon="info")]
        return (decode_layout(compose_screen(bw, cards, fonts, SETTINGS["en"], now).layout),
                decode_layout(compose_screen(bw, rows, fonts, SETTINGS["en"], now).layout))

    def chips(layout: Any) -> list[Rect]:
        return [c for c in layout.commands if isinstance(c, Rect) and c.border == 0 and c.h > 3 and c.w > 8]

    def squares(layout: Any, filled: bool) -> list[Rect]:
        return [c for c in layout.commands if isinstance(c, Rect) and c.w == c.h == tokens.marker
                and (c.border == 0) == filled]

    row_size = FontContext.for_fontset(fonts).nearest_size(tokens.row_title.size)
    for urgent in (screen("error"), screen("info", "needs_input")):
        calm = screen("info")
        assert chips(urgent[0]) and not chips(calm[0])
        if tokens.marker:
            assert squares(urgent[1], filled=True) and not squares(calm[1], filled=True)
        elif has_bold(fonts, row_size):
            def bold_rows(layout: Any) -> list[Glyphs]:
                return [c for c in layout.commands if isinstance(c, Glyphs) and c.size_px == row_size
                        and FontContext.for_fontset(fonts).regular_of(c.face) != c.face]
            assert bold_rows(urgent[1]) and not bold_rows(calm[1])


@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_calm_screens_use_red_only_in_chrome(fonts: Any, now: datetime, panel: TagPanel) -> None:
    """Without an alert the one red is the masthead's accent rule (none at all without a masthead)."""
    sets = card_sets(now)
    for name in ("calm", "test-card", "empty"):
        layout = decode_layout(compose_screen(panel, sets[name], fonts, SETTINGS["en"], now).layout)
        red = [c for c in layout.commands if getattr(c, "color", None) == RED]
        assert len(red) <= 1 and bool(red) == bool(layout.flags & 1), name
        if red:
            (rule,) = red
            assert isinstance(rule, Rect) and rule.border == 0 and 2 <= rule.h <= 3 and rule.w > layout.width // 2
            w, h, px = colours(layout, fonts, panel)
            assert all(i // w < rule.y + rule.h for i, v in enumerate(px) if v == RED), name
        else:
            assert 12 not in FontContext.for_fontset(fonts).text_sizes() and tokens_for(
                layout.width, layout.height).cls == XS  # the masthead is missing only there


# --------------------------------------------------------------------------- collisions and room


def painted(cmd: Any) -> set[tuple[int, int]]:
    """Pixels a RECT paints (an outline only its band)."""
    full = {(cmd.x + i, cmd.y + j) for i in range(cmd.w) for j in range(cmd.h)}
    if not cmd.border:
        return full
    b = cmd.border
    return {(x, y) for x, y in full if x < cmd.x + b or x >= cmd.x + cmd.w - b or y < cmd.y + b
            or y >= cmd.y + cmd.h - b}


@pytest.mark.parametrize("lang", sorted(SETTINGS))
@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_no_text_collides(fonts: Any, now: datetime, panel: TagPanel, lang: str) -> None:
    """Texts, icons, the QR code and progress bars never share a pixel; dark text never lands on a rule,
    marker or chip (white text sits in its chip); a QR keeps its quiet zone; all ink stays on the canvas."""
    for name, screen in screens(panel, fonts, SETTINGS[lang], now):
        layout = decode_layout(screen.layout)
        content = [(c, ink(c, fonts)) for c in layout.commands if isinstance(c, Glyphs | Icon | Qr | Progress)]
        shapes = set().union(*(painted(c) for c in layout.commands if isinstance(c, Rect)))
        everything = set().union(shapes, *(p for _, p in content))
        assert all(0 <= x < layout.width and 0 <= y < layout.height for x, y in everything), name
        for i, (a, pa) in enumerate(content):
            for b, pb in content[i + 1:]:
                assert not pa & pb, (name, a, b)
            if getattr(a, "color", None) != WHITE and not isinstance(a, Progress):
                assert not pa & shapes, (name, a)
            if isinstance(a, Qr):
                xs, ys = [x for x, _ in pa], [y for _, y in pa]
                q = QR_QUIET * a.module_px
                zone = {(x, y) for x in range(min(xs) - q, max(xs) + q + 1)
                        for y in range(min(ys) - q, max(ys) + q + 1)}
                assert not (everything - pa) & zone, (name, "QR quiet zone")


@pytest.mark.parametrize("panel", [HEMA_PORTRAIT, TagPanel(0x0BADCAFE, 250, 122, 2, 3, 1, "Tiny portrait"), HEMA],
                         ids=panel_id)
def test_rows_say_enough_to_count_as_shown(fonts: Any, dev_fonts: Any, now: datetime, panel: TagPanel) -> None:
    """A list row's title shows all of it or at least 8 characters; one that cannot (a narrow portrait, a large
    old-pack size) wraps to a second line or waits under "+N MORE" — never a row that says next to nothing."""
    settings = SETTINGS["en"]
    for pack in (fonts, dev_fonts):
        tokens = tokens_for(*logical_size(panel))
        c = Canvas(panel, pack, settings.language, tokens, screen_module._limits())
        views = ordered_views(example_cards(now), settings)
        grid = Grid.of(c)
        room = len(views) * grid.pitch  # a slot per card: no spare room that would wrap cut titles anyway
        listing = choose_rows(c, grid, views[1:], tokens.content_width, room, tokens.rows[0], now, settings)
        assert listing.rows and all(legible_row(row.title) for row in listing.rows)
        shown = compose_screen(panel, example_cards(now), pack, settings, now).delivery_ids
        assert set(shown[1:]) <= {r.view.delivery_id for r in listing.rows}


@pytest.mark.parametrize("panel", [*PANELS, TagPanel(0x0BADCAFE, 296, 128, 1, 1, 0, "Small")], ids=panel_id)
def test_density_targets(fonts: Any, dev_fonts: Any, now: datetime, panel: TagPanel) -> None:
    """Smaller, sharper type shows more at a glance: at least the panel's target of the example cards, on the
    pack under test and on the old-shape dev pack."""
    for pack in (fonts, dev_fonts):
        screen = compose_screen(panel, example_cards(now), pack, SETTINGS["en"], now)
        assert len(screen.delivery_ids) >= density(panel, pack)
        assert screen.delivery_ids == ORDER[:len(screen.delivery_ids)]


@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_ink_coverage_is_moderate(fonts: Any, now: datetime, panel: TagPanel) -> None:
    """No large dark areas: at most about a third of a composed screen is ink (identify excepted)."""
    for lang in ("en", "en-excerpts-qr"):
        for name, screen in screens(panel, fonts, SETTINGS[lang], now):
            if name == "identify":
                continue
            w, h, px = colours(screen.layout, fonts, panel)
            assert sum(v != WHITE for v in px) <= 0.35 * w * h, name


def test_a_lone_card_is_centred(fonts: Any, now: datetime) -> None:
    """One card alone sits in the middle of the white under the masthead."""
    note = card_sets(now)["test-card"]
    for panel in (HEMA, LANDSCAPE_BWR, PORTRAIT_BWR, SMALL_S):
        layout = decode_layout(compose_screen(panel, note, fonts, SETTINGS["en"], now).layout)
        rules = [c for c in layout.commands if isinstance(c, Rect) and c.h <= 3 and c.w > layout.width // 2]
        top = rules[0].y + rules[0].h if rules else 0
        below = [y for c in layout.commands if isinstance(c, Glyphs | Icon) for _, y in ink(c, fonts) if y >= top]
        assert abs((min(below) - top) - (layout.height - 1 - max(below))) <= 8


def test_overflow_says_how_many_wait(fonts: Any, now: datetime) -> None:
    """Cards left over are counted in the last slot: "+N MORE" in bold letter-spaced caps."""
    cards = card_sets(now)["dense"]
    for panel in (HEMA, SMALL_S, LANDSCAPE_BWR):
        screen = compose_screen(panel, cards, fonts, SETTINGS["en"], now)
        assert screen.pending_count > 0
        text = strings.more(screen.pending_count)
        c = Canvas(panel, fonts, "en", tokens_for(*logical_size(panel)), screen_module._limits())
        block = c.chrome(text, 400, c.resolve(CAPS_12, text))
        want = tuple(g.glyph_id for g in block.glyphs)
        runs = [tuple(g.glyph_id for g in cmd.glyphs) for cmd in decode_layout(screen.layout).commands
                if isinstance(cmd, Glyphs)]
        assert want in runs


# --------------------------------------------------------------------------- type


@pytest.mark.parametrize("script", sorted(LONG))
@pytest.mark.parametrize("panel", [HEMA, LANDSCAPE_BWR], ids=panel_id)
def test_small_sizes_only_where_the_script_has_them(fonts: Any, now: datetime, script: str,
                                                    panel: TagPanel) -> None:
    """Text is never smaller than its script's legibility floor (Arabic, Thai, kana, Hangul 14 px; Han, Indic
    and the rest 16 px), and Latin rows use the smallest size the pack has."""
    ctx = FontContext.for_fontset(fonts)
    text = LONG[script].strip()[:60]
    settings = dataclasses.replace(SETTINGS["en"], language="ar" if script == "arabic" else "en")
    cards = [card(i, "notification", text, 50 - i, i, now) for i in range(1, 5)]
    screen = compose_screen(panel, cards, fonts, settings, now)
    layout = check(screen, panel, fonts)
    assert screen.unsupported_chars == ()
    floor = legible_size(text)
    least = min((s for s in ctx.text_sizes() if s >= floor), default=max(ctx.text_sizes()))
    runs = [c for c in layout.commands if isinstance(c, Glyphs)]
    for cmd in runs:
        if not ctx.grid_fit(cmd.face) and cmd.face != ctx.emoji_face:  # the card's script, not chrome or emoji
            assert cmd.size_px >= least, (cmd.face, cmd.size_px)
    if script in ("latin", "narrow") and tokens_for(*logical_size(panel)).cls == XS:
        assert min(c.size_px for c in runs) == min(ctx.text_sizes())


# --------------------------------------------------------------------------- fallbacks


@pytest.mark.parametrize("lang", ["en", "ar"])
@pytest.mark.parametrize("panel", [*PANELS, TagPanel(0x0BADCAFE, 250, 122, 1, 1, 1, "Tiny portrait")],
                         ids=panel_id)
def test_old_pack_fallback(fonts: Any, dev_fonts: Any, now: datetime, panel: TagPanel, lang: str) -> None:
    """No exception and a valid screen on every class, left-to-right and right-to-left, on the dev pack (no
    12/14 px, no bold) and on the pack under test (the production pack with ``CREMIND_TAG_TEST_PACK``): sizes
    resolve to strikes the pack has and the shown cards stay a prefix of the display order."""
    for pack in (dev_fonts, fonts):
        sizes = set(FontContext.for_fontset(pack).text_sizes())
        for name, screen in screens(panel, pack, SETTINGS[lang], now):
            layout = check(screen, panel, pack)
            assert {c.size_px for c in layout.commands if isinstance(c, Glyphs)} <= sizes, name
            if name == "example":
                assert screen.delivery_ids == ORDER[:len(screen.delivery_ids)]


@pytest.mark.parametrize("panel", [HEMA, HEMA_BW, HEMA_PORTRAIT], ids=["bwr", "bw", "portrait"])
def test_hema_screens(fonts: Any, dev_fonts: Any, now: datetime, panel: TagPanel) -> None:
    """The user's 2.13" tag: a masthead with the accent rule when the pack has 12 px text (else a footer row with
    the time under a hairline), at least the density target, and a new minute changes the screen."""
    for pack in (fonts, dev_fonts):
        screen = compose_screen(panel, example_cards(now), pack, SETTINGS["en"], now)
        layout = check(screen, panel, pack)
        accent = RED if panel.planes == 2 else BLACK
        rules = [c for c in layout.commands if isinstance(c, Rect) and c.border == 0 and c.w > layout.width // 2]
        if 12 in FontContext.for_fontset(pack).text_sizes():
            assert any(r.h == 2 and r.y < 24 and r.color == accent for r in rules)
        else:
            assert any(r.h == 1 and r.y > layout.height - 30 and r.color == BLACK for r in rules)
            assert not any(r.h == 2 for r in rules)
        assert len(screen.delivery_ids) >= density(panel, pack)
        later = compose_screen(panel, example_cards(now), pack, SETTINGS["en"], now + timedelta(minutes=1))
        assert later.layout != screen.layout
