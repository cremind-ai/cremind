"""Screen composition with the real pack: model, panels, limits, degradation, determinism, speed."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.tags.runtime.compose import screen as screen_module
from app.tags.runtime.compose.api import ComposedScreen, ScreenSettings, TagPanel
from app.tags.runtime.compose.samples import example_cards
from app.tags.runtime.compose.screen import (
    compose_blank,
    compose_identify,
    compose_screen,
    compose_setup_code,
    logical_size,
    plans_for,
)
from app.tags.runtime.compose.tokens import tokens_for
from app.tags.runtime.layout import FontContext
from app.tags.runtime.layout import engine as engine_module
from app.tags.runtime.protocol.ids import LAYOUT_MAX_COMMANDS, Color, NodeRole
from app.tags.runtime.protocol.layout import Glyphs, Icon, Progress, Qr, Rect, check_panel, decode_layout
from app.tags.runtime.secure.codes import SetupPayload

from .design_checks import (
    HEMA,
    HEMA_BW,
    HEMA_PORTRAIT,
    LANDSCAPE_BW,
    LANDSCAPE_BWR,
    LARGE,
    LONG,
    ORDER,
    PORTRAIT_BWR,
    SHELF,
    SMALL_S,
    card,
    check,
    density,
    render_cost,
)

pytestmark = pytest.mark.fonts

PANELS = [
    LANDSCAPE_BW, LANDSCAPE_BWR, PORTRAIT_BWR,
    TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 2, "Upside down"),
    TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 3, "Portrait 3"),
    SHELF,
    TagPanel(0x0BADCAFE, 250, 122, 2, 3, 1, "Tiny portrait"),
    HEMA, HEMA_BW, HEMA_PORTRAIT, SMALL_S,
]
def panel_id(p: TagPanel) -> str:
    return f"{p.width}x{p.height}-r{p.rotation}-p{p.planes}"


# --------------------------------------------------------------------------- model


@pytest.mark.parametrize("panel", PANELS, ids=panel_id)
def test_screens_validate_on_every_panel(fonts: Any, now: datetime, panel: TagPanel) -> None:
    for settings in (ScreenSettings(), ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "vi"),
                     ScreenSettings(True, True, "Asia/Riyadh", "ar")):
        screen = compose_screen(panel, example_cards(now), fonts, settings, now)
        layout = check(screen, panel, fonts)
        expect = (panel.width, panel.height) if panel.rotation % 2 == 0 else (panel.height, panel.width)
        assert (layout.width, layout.height, layout.rotation) == (*expect, panel.rotation)
        assert screen.delivery_ids[0] == 501  # highest priority first


@pytest.mark.parametrize("panel", [LANDSCAPE_BW, PORTRAIT_BWR, HEMA, HEMA_PORTRAIT, SHELF, SMALL_S, LARGE],
                         ids=panel_id)
def test_delivery_ids_shown_and_pending(fonts: Any, now: datetime, panel: TagPanel) -> None:
    """Shown cards are a prefix of the display order (at least the panel's density target), the rest pending."""
    notices = [card(600 + i, "notification", f"Notice number {i}", 20, 100 + i, now) for i in range(12)]
    cards = example_cards(now) + notices
    order = ORDER + tuple(600 + i for i in range(12))  # priority 20, newest first: more than any panel shows
    screen = compose_screen(panel, cards, fonts, ScreenSettings(), now)
    n = len(screen.delivery_ids)
    assert screen.delivery_ids == order[:n] and screen.pending_delivery_ids == order[n:]
    assert screen.pending_count == len(order) - n > 0
    assert n >= density(panel, fonts)
    resolved = [*cards, card(900, "resolved", "Answered", 90, 0, now)]
    assert compose_screen(panel, resolved, fonts, ScreenSettings(), now).delivery_ids == screen.delivery_ids


def test_single_card_and_empty_screen(fonts: Any, now: datetime) -> None:
    one = compose_screen(LANDSCAPE_BW, example_cards(now)[:1], fonts, ScreenSettings(), now)
    assert one.delivery_ids == (501,) and one.pending_count == 0
    empty = compose_screen(LANDSCAPE_BW, [], fonts, ScreenSettings(), now)
    layout = check(empty, LANDSCAPE_BW, fonts)
    assert empty.delivery_ids == () and any(isinstance(c, Icon) for c in layout.commands)


def test_red_only_on_two_plane_panels(fonts: Any, now: datetime) -> None:
    cards = example_cards(now)
    bwr = decode_layout(compose_screen(LANDSCAPE_BWR, cards, fonts, ScreenSettings(), now).layout)
    bw = decode_layout(compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(), now).layout)
    assert bwr.flags & 1 and any(getattr(c, "color", 0) == Color.RED for c in bwr.commands)
    assert not bw.flags & 1 and all(getattr(c, "color", 0) != Color.RED for c in bw.commands)
    # A calm screen's one red is the masthead's accent rule: a RECT at most 3 px high, near the top.
    calm = [card(1, "notification", "All good", 40, 1, now)]
    calm_layout = decode_layout(compose_screen(LANDSCAPE_BWR, calm, fonts, ScreenSettings(), now).layout)
    red = [c for c in calm_layout.commands if getattr(c, "color", 0) == Color.RED]
    assert calm_layout.flags & 1 and len(red) == 1
    (rule,) = red
    assert isinstance(rule, Rect) and rule.border == 0 and rule.h <= 3 and rule.y < calm_layout.height // 4


def _glyph_total(screen: ComposedScreen) -> int:
    return sum(len(c.glyphs) for c in decode_layout(screen.layout).commands if isinstance(c, Glyphs))


def test_body_only_with_excerpts(fonts: Any, now: datetime) -> None:
    cards = [card(1, "excerpt", "Weekly summary", 50, 1, now, body="Shipped the companion and fixed bugs.")]
    off = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(show_excerpts=False), now)
    on = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(show_excerpts=True), now)
    assert _glyph_total(on) > _glyph_total(off) + 25


def test_qr_only_for_valid_links_when_enabled(fonts: Any, now: datetime) -> None:
    link = "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"
    cards = [card(1, "needs_input", "Approve?", 90, 1, now, link=link)]

    def qrs(settings: ScreenSettings, cs: list[Any]) -> list[Qr]:
        return [c for c in decode_layout(compose_screen(LANDSCAPE_BW, cs, fonts, settings, now).layout).commands
                if isinstance(c, Qr)]

    found = qrs(ScreenSettings(qr_links=True), cards)
    assert len(found) == 1 and found[0].text == link.encode()
    assert qrs(ScreenSettings(qr_links=False), cards) == []
    bad = [card(1, "needs_input", "Approve?", 90, 1, now, link=link + "?token=abc")]
    assert qrs(ScreenSettings(qr_links=True), bad) == []


def test_progress_bar_needs_real_counts(fonts: Any, now: datetime) -> None:
    def bars(progress: Any) -> list[Progress]:
        cs = [card(1, "progress", "Backup", 30, 1, now, progress=progress)]
        layout = decode_layout(compose_screen(LANDSCAPE_BW, cs, fonts, ScreenSettings(), now).layout)
        return [c for c in layout.commands if isinstance(c, Progress)]

    (bar,) = bars({"done": 7, "total": 12})
    assert (bar.value, bar.max) == (7, 12) and bar.w > 100
    (big,) = bars({"done": 50_000, "total": 200_000})
    assert big.max <= 0xFFFF and abs(big.value / big.max - 0.25) < 0.01
    assert bars(None) == [] and bars({"done": 1, "total": 0}) == []


def test_progress_fills_from_the_start_side_in_rtl(fonts: Any, now: datetime) -> None:
    """PROGRESS fills left to right; a right-to-left UI gets the same frame and a fill anchored on the right."""
    cs = [card(1, "progress", "Backup", 30, 1, now, progress={"done": 3, "total": 4})]
    layout = decode_layout(compose_screen(LANDSCAPE_BW, cs, fonts, ScreenSettings(language="ar"), now).layout)
    assert not any(isinstance(c, Progress) for c in layout.commands)
    frames = [c for c in layout.commands if isinstance(c, Rect) and c.border == 1]
    (frame,) = [f for f in frames if f.w > 100]
    (fill,) = [c for c in layout.commands if isinstance(c, Rect) and c.border == 0 and c.y == frame.y + 2]
    assert fill.x + fill.w == frame.x + frame.w - 2 and fill.h == frame.h - 4
    assert fill.w == (frame.w - 4) * 3 // 4 and frame.x + frame.w > layout.width // 2  # on the start (right) side


def test_identify_and_blank(fonts: Any) -> None:
    largest = max(s for s in FontContext.for_fontset(fonts).text_sizes() if s <= 32)
    for panel in (LANDSCAPE_BW, PORTRAIT_BWR, TagPanel(0x1A2B3C4D, 296, 128, 1, 1, 0, ""), HEMA, HEMA_PORTRAIT):
        ident = compose_identify(panel, fonts)
        layout = check(ident, panel, fonts)
        assert ident.delivery_ids == () and any(isinstance(c, Glyphs) and c.size_px == largest
                                                for c in layout.commands)
        frame = layout.commands[0]
        assert isinstance(frame, Rect) and frame.border and (frame.x, frame.y, frame.w, frame.h) == (
            0, 0, layout.width, layout.height)
        assert frame.color == (Color.RED if panel.planes == 2 else Color.BLACK)
        blank = compose_blank(panel)
        layout = decode_layout(blank.layout)
        assert layout.commands == () and layout.background == Color.WHITE
        check_panel(layout, panel.width, panel.height)
    long_name = TagPanel(1, 400, 300, 1, 1, 0, "Phòng họp " * 40)
    check(compose_identify(long_name, fonts, "CAFE0001"), long_name, fonts)


SETUP_PANELS = [HEMA, HEMA_PORTRAIT, SHELF, SMALL_S, LANDSCAPE_BWR, PORTRAIT_BWR, LARGE]


@pytest.mark.parametrize("panel", SETUP_PANELS, ids=panel_id)
def test_setup_code_fits_every_panel(fonts: Any, dev_fonts: Any, panel: TagPanel) -> None:
    """A released tag's setup code fits every panel, the 2.13" ones included (the old composer raised there)."""
    payload = SetupPayload(NodeRole.TAG, panel.tag_id, bytes(range(10)))
    for pack in (fonts, dev_fonts):
        screen = compose_setup_code(panel, pack, payload.code(), payload.qr_text())
        layout = check(screen, panel, pack)
        (qr,) = [c for c in layout.commands if isinstance(c, Qr)]
        assert qr.text == payload.qr_text().encode("ascii")
        symbols = payload.code().replace("-", "")  # two groups per line: the dashes between lines go
        assert sum(len(c.glyphs) for c in layout.commands if isinstance(c, Glyphs)) >= len(symbols)


def test_dev_pack_without_32px(dev_fonts: Any, now: datetime) -> None:
    cards = [c for c in example_cards(now) if c.delivery_id in (501, 506)]
    for panel in (LANDSCAPE_BWR, PORTRAIT_BWR):
        screen = compose_screen(panel, cards, dev_fonts, ScreenSettings(True, True), now)
        layout = check(screen, panel, dev_fonts)
        assert all(c.size_px != 32 for c in layout.commands if isinstance(c, Glyphs))
        assert screen.delivery_ids == (501, 506)
        assert set(screen.unsupported_chars) >= {"今", "日"}  # shown texts only; the dev pack has no CJK
    check(compose_identify(LANDSCAPE_BW, dev_fonts), LANDSCAPE_BW, dev_fonts)


# --------------------------------------------------------------------------- worst cases


def worst_cards(script: str, now: datetime) -> list[Any]:
    text = (LONG[script] * 40)[:400]
    body = (LONG[script] * 60)[:1200]
    link = "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"
    return [card(i, "excerpt" if i % 2 else "needs_input", text, 50 + (i % 5), i, now, body=body, link=link,
                 severity="error") for i in range(1, 21)]


@pytest.mark.parametrize("script", sorted(LONG))
@pytest.mark.parametrize("panel", [LANDSCAPE_BWR, PORTRAIT_BWR, SHELF, LARGE, HEMA, HEMA_PORTRAIT, SMALL_S],
                         ids=["landscape", "portrait", "small", "large", "hema", "hema-portrait", "s"])
def test_worst_case_screens_stay_within_limits(fonts: Any, now: datetime, script: str, panel: TagPanel) -> None:
    settings = ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "ar" if script == "arabic" else "en")
    screen = compose_screen(panel, worst_cards(script, now), fonts, settings, now)
    check(screen, panel, fonts)
    assert len(screen.delivery_ids) >= 1 and len(screen.delivery_ids) + screen.pending_count == 20
    assert screen.unsupported_chars == ()


MAX_CANVAS = [LARGE, TagPanel(0x1A2B3C4D, 2048, 2048, 2, 3, 0, "Largest"),
              TagPanel(0x1A2B3C4D, 2048, 480, 2, 3, 1, "Widest portrait")]


@pytest.mark.parametrize("panel", MAX_CANVAS, ids=["800x480", "2048x2048", "480x2048"])
def test_render_cost_bounds_on_the_largest_canvases(fonts: Any, now: datetime, panel: TagPanel) -> None:
    """§4.3 render cost: the worst screens the composer makes, on canvases up to LAYOUT_MAX_SIDE, stay far
    inside LAYOUT_MAX_QR and LAYOUT_MAX_LINE_STEPS (one QR code; rules and frames are RECTs, no LINE)."""
    link = "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"
    text = (LONG["alternating"] * 40)[:400]
    cards = [card(i, "needs_input", text, 50 + i % 5, i, now, body=text * 3, link=link, progress={"done": i, "total": 20})
             for i in range(1, 21)]
    for settings in (ScreenSettings(True, True), ScreenSettings(True, True, "Asia/Riyadh", "ar")):
        layout = check(compose_screen(panel, cards, fonts, settings, now), panel, fonts)
        qrs, steps = render_cost(layout)
        assert qrs == 1 and steps <= 3 * layout.width
        layout = check(compose_identify(panel, fonts), panel, fonts)
        assert render_cost(layout) == (0, 0)


def test_composer_budget_counts_render_cost(fonts: Any, now: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget refuses a QR code beyond LAYOUT_MAX_QR: the composer then leaves it out rather than emit an
    invalid layout."""
    link = "https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"
    cards = [card(1, "needs_input", "Approve?", 90, 1, now, link=link)]
    assert render_cost(decode_layout(compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(qr_links=True),
                                                    now).layout))[0] == 1
    monkeypatch.setattr(screen_module, "LAYOUT_MAX_QR", 0)
    screen = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(qr_links=True), now)
    assert render_cost(decode_layout(screen.layout))[0] == 0 and screen.delivery_ids == (1,)


def _usage(screen: ComposedScreen, limit: str) -> int:
    layout = decode_layout(screen.layout)
    return {"LAYOUT_MAX_GLYPHS": sum(len(c.glyphs) for c in layout.commands if isinstance(c, Glyphs)),
            "MAX_BYTES": len(screen.layout), "LAYOUT_MAX_COMMANDS": len(layout.commands)}[limit]


@pytest.mark.parametrize("limit", ["LAYOUT_MAX_GLYPHS", "MAX_BYTES", "LAYOUT_MAX_COMMANDS"])
@pytest.mark.parametrize("panel", [LANDSCAPE_BW, HEMA], ids=["400x300", "hema"])
def test_degradation_ladder_under_tight_limits(fonts: Any, now: datetime, monkeypatch: pytest.MonkeyPatch,
                                               limit: str, panel: TagPanel) -> None:
    """A limit tightened to what a later plan of the ladder uses: the composer takes exactly that plan."""
    cards = example_cards(now)
    settings = ScreenSettings(False, True)  # no body: what each plan uses is fixed
    full_ladder = plans_for
    full = compose_screen(panel, cards, fonts, settings, now)
    later = full
    for skip in range(1, len(full_ladder(tokens_for(*logical_size(panel))))):
        monkeypatch.setattr(screen_module, "plans_for", lambda t, skip=skip: full_ladder(t)[skip:])
        later = compose_screen(panel, cards, fonts, settings, now)
        if _usage(later, limit) < _usage(full, limit):
            break
    monkeypatch.setattr(screen_module, "plans_for", full_ladder)
    assert _usage(later, limit) < _usage(full, limit)

    monkeypatch.setattr(screen_module, limit, _usage(later, limit))
    tight = compose_screen(panel, cards, fonts, settings, now)
    check(tight, panel, fonts)
    assert tight == later
    assert tight.delivery_ids == full.delivery_ids[:len(tight.delivery_ids)]
    assert tight.pending_count == len(cards) - len(tight.delivery_ids)
    assert compose_screen(panel, cards, fonts, settings, now) == tight  # deterministic


def test_plans_degrade_deterministically(fonts: Any, now: datetime) -> None:
    text = ("aب1אกक" * 80)[:400]  # a strike change on every character: one GLYPHS command per glyph
    cards = [card(i, "excerpt", text, 50, i, now, body=text * 3) for i in range(1, 6)]
    a = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(True), now)
    engine_module._cache.clear()
    b = compose_screen(LANDSCAPE_BW, cards, fonts, ScreenSettings(True), now)
    assert a == b
    layout = check(a, LANDSCAPE_BW, fonts)
    assert len(layout.commands) <= LAYOUT_MAX_COMMANDS
    tokens = tokens_for(400, 300)
    plans = plans_for(tokens)
    assert plans[0].rows == tokens.rows[0] and plans[0].ornaments and not plans[1].ornaments
    assert plans[-1].rows == 0 and plans[-1].header == "none" and not plans[-1].body and not plans[-1].qr


@pytest.mark.parametrize("panel", [PORTRAIT_BWR, HEMA], ids=panel_id)
def test_same_input_same_bytes(fonts: Any, now: datetime, panel: TagPanel) -> None:
    cards = example_cards(now)
    settings = ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "vi")
    first = compose_screen(panel, cards, fonts, settings, now)
    engine_module._cache.clear()
    assert compose_screen(panel, list(reversed(cards)), fonts, settings, now) == first
    later = compose_screen(panel, cards, fonts, settings, now + timedelta(minutes=1))
    assert later.layout != first.layout  # the time the screen was made changed


@pytest.mark.parametrize(("panel", "extra"), [(LANDSCAPE_BWR, 0), (HEMA, 5)], ids=["400x300", "hema-12-cards"])
def test_typical_screen_under_200ms(fonts: Any, now: datetime, panel: TagPanel, extra: int) -> None:
    cards = example_cards(now) + [card(700 + i, "notification", f"Update number {i}", 40, 60 + i, now)
                                  for i in range(extra)]
    settings = ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "en")
    compose_screen(panel, cards, fonts, settings, now)  # warm-up: pack, cmaps, HarfBuzz faces
    timings = []
    for minute in range(1, 6):
        engine_module._cache.clear()  # no layout cache: every text is shaped again
        started = time.perf_counter()
        compose_screen(panel, cards, fonts, settings, now + timedelta(minutes=minute))
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.2, timings
