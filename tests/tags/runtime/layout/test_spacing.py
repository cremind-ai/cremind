"""Spacing with the real pack: grid-fitted positions, kerning, marks, line pitch, tracking, bold, cache keys.

Expected values come from whichever pack is loaded (its hinted advances and
HarfBuzz's own output), so the tests hold for the Windows and the Linux build
and for packs without 12/14 px or a bold face.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest
import uharfbuzz as hb

from app.tags.runtime.layout import FontContext, layout_text, measure_text
from app.tags.runtime.layout import engine as engine_module
from app.tags.runtime.layout.unicode import nfc

pytestmark = pytest.mark.fonts

LATIN = "Approve deployment of release 2.4 to production? Telegram channel stopped. Weekly summary, 7/12 done."
LONG = (LATIN + " Reply ready: Tóm tắt cuộc họp sáng nay — Người dùng đã yêu cầu phê duyệt. ") * 3
DEVANAGARI = "नमस्ते दुनिया! क्षत्रिय स्त्री हिन्दी।"
ARABIC = "السلام عليكم، كيف حالك اليوم؟ لا بأس"
THAI = "สวัสดีครับ วันนี้อากาศดีมากไปเที่ยวกันไหม"
HEBREW = "שלום עולם מה שלומך היום"  # no "!" / "?": Noto Sans Hebrew lacks them (Noto Sans, which has a Bold, draws them)


def ctx_of(fonts: Any) -> Any:
    return FontContext.for_fontset(fonts)


def weights(fonts: Any, size: int) -> list[str]:
    """The weights the pack can draw Latin at ``size`` (bold only with Noto Sans Bold's strike)."""
    return ["regular", "bold"] if ctx_of(fonts).has_weight("bold", size) else ["regular"]


def latin_face(ctx: Any, weight: str, size: int) -> int:
    """The face drawing Latin at ``weight``: Noto Sans, or its bold sibling."""
    return ctx.styled(1, weight, size, [ord("A")])


def shaped(ctx: Any, face: int, size: int, text: str) -> list[Any]:
    """HarfBuzz's own output for ``text`` as one LTR Latin run: (gid, x_advance, x_offset)."""
    buf = hb.Buffer()
    buf.add_str(text)
    buf.direction = "ltr"
    buf.script = "Latn"
    buf.language = "en"
    buf.flags = hb.BufferFlags(int(hb.BufferFlags.BOT) | int(hb.BufferFlags.EOT))
    hb.shape(ctx.hb_font(face, size), buf, {})
    return [(i.codepoint, p.x_advance, p.x_offset) for i, p in zip(buf.glyph_infos, buf.glyph_positions, strict=True)]


def px(v: int) -> int:
    return (v + 32) >> 6


def bases(block: Any) -> dict[int, list[Any]]:
    """Glyphs per cluster (code-point index), in drawing order: the base first, then its marks."""
    out: dict[int, list[Any]] = defaultdict(list)
    for g in block.glyphs:
        out[g.cluster].append(g)
    return out


def ink_pixels(ctx: Any, g: Any) -> set[tuple[int, int]]:
    bitmap = ctx.glyph(g.face_id, g.size_px, g.glyph_id)
    if bitmap is None or bitmap.empty:
        return set()
    row_bytes = (bitmap.width + 7) // 8
    x0, y0 = g.x + bitmap.bearing_x, g.y - bitmap.bearing_y
    return {(x0 + c, y0 + r) for r in range(bitmap.height) for c in range(bitmap.width)
            if bitmap.bitmap[r * row_bytes + (c >> 3)] & (0x80 >> (c & 7))}


def ink_box(ctx: Any, g: Any) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of a positioned glyph's ink, absolute."""
    left, top, right, bottom = ctx.ink(g.face_id, g.size_px, g.glyph_id)
    return g.x + left, g.y + top, g.x + right, g.y + bottom


# --------------------------------------------------------------------------- grid-fitted positions


def test_latin_steps_are_hinted_advances_plus_rounded_kerning(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    for size in ctx.text_sizes():
        for weight in weights(fonts, size):
            block = layout_text(LATIN, fonts, width=8000, size_px=size, language="en", weight=weight)
            face = latin_face(ctx, weight, size)
            assert block.faces == (face,)
            advances = ctx.advances(face, size)
            pen, expected = 0, []
            for gid, xa, xo in shaped(ctx, face, size, LATIN):
                if ctx.ink(face, size, gid) is not None:
                    expected.append(pen + px(xo))
                pen += advances[gid] + px(xa - ctx.nominal_advance(face, size, gid))
            assert [g.x for g in block.glyphs] == expected, (size, weight)
            assert block.width == pen  # whole pixels: the line is exactly the sum of its advances


def test_the_same_pair_always_gets_the_same_gap(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    varied_without_fitting = False
    for size in ctx.text_sizes():
        for weight in weights(fonts, size):
            for grid_fit in (True, False):
                block = layout_text(LONG, fonts, width=300, size_px=size, language="vi", weight=weight,
                                    grid_fit=grid_fit)
                assert len(block.lines) > 3
                clusters = bases(block)
                steps: dict[tuple, set[int]] = defaultdict(set)
                for a, b in zip(sorted(clusters), sorted(clusters)[1:], strict=False):
                    left, right = clusters[a], clusters[b]
                    if left[0].line != right[0].line or any(ch.isspace() for ch in block.text[a + 1:b]):
                        continue  # adjacent letters only: same line, no space between
                    steps[(tuple(g.glyph_id for g in left), right[0].glyph_id)].add(right[0].x - left[0].x)
                varied = {pair: gaps for pair, gaps in steps.items() if len(gaps) > 1}
                if grid_fit:
                    assert varied == {}, (size, weight)
                else:
                    varied_without_fitting |= bool(varied)
    assert varied_without_fitting, "HarfBuzz's 26.6 positions should vary somewhere (else this test proves nothing)"


@pytest.mark.parametrize("text", ["AVAV", "Te Te", "To Ty"])
def test_kerning_is_rounded_per_pair(fonts: Any, text: str) -> None:
    ctx = ctx_of(fonts)
    kerned = False
    for size in ctx.text_sizes():
        for weight in weights(fonts, size):
            face = latin_face(ctx, weight, size)
            xs = [g.x for g in layout_text(text, fonts, width=1000, size_px=size, weight=weight).glyphs]
            run = shaped(ctx, face, size, text)
            advances = ctx.advances(face, size)
            pen, want = 0, []
            for gid, xa, _xo in run:
                if ctx.ink(face, size, gid) is not None:
                    want.append(pen)
                kern = xa - ctx.nominal_advance(face, size, gid)
                kerned |= px(kern) != 0
                pen += advances[gid] + px(kern)
            assert xs == want, (text, size, weight)
            if text == "AVAV":
                assert xs[1] - xs[0] == xs[3] - xs[2]  # the same pair, the same step
    assert kerned, f"{text!r} should be kerned by at least a pixel at some size"


def test_glyphs_never_overlap_in_tight_pairs(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    text = "ill rn mm ff LT Ty .,:;|!ij' Approve deployment"
    for size in ctx.text_sizes():
        for weight in weights(fonts, size):
            block = layout_text(text, fonts, width=2000, size_px=size, weight=weight)
            clusters = bases(block)
            order = sorted(clusters)
            for a, b in zip(order, order[1:], strict=False):
                left = set().union(*(ink_pixels(ctx, g) for g in clusters[a]))
                right = set().union(*(ink_pixels(ctx, g) for g in clusters[b]))
                assert not left & right, (size, weight, block.text[a], block.text[b])


def test_marks_keep_their_offset_from_the_base(fonts: Any) -> None:
    ctx = ctx_of(fonts)

    def offsets(block: Any, cluster: int) -> list[tuple[int, int, int]]:
        base, *marks = bases(block)[cluster]
        return [(m.glyph_id, m.x - base.x, m.y - base.y) for m in marks]

    for size in ctx.text_sizes():
        for cluster in ("x̂́", "q̃", "ọ", "ỗ", "ặ"):
            alone = layout_text(cluster, fonts, width=200, size_px=size, language="vi")
            # At a whole-pixel pen both paths round the same offsets: identical glyphs.
            assert alone.glyphs == layout_text(cluster, fonts, width=200, size_px=size, language="vi",
                                               grid_fit=False).glyphs
            want = offsets(alone, 0)
            for prefix in ("ab ", "Tiếng Việt: hô ", "Wy "):
                block = layout_text(prefix + cluster, fonts, width=2000, size_px=size, language="vi")
                at = len(nfc(prefix))
                assert offsets(block, at) == want, (size, cluster, prefix)  # rounded once, wherever it sits
        # Vietnamese marks sit on their base: inside its ink box, give or take a pixel.
        block = layout_text("họp cuộc ọ ụ ị ẹ", fonts, width=2000, size_px=size, language="vi")
        assert block.notdef == 0
        for glyphs in bases(block).values():
            base, *marks = glyphs
            bl, _bt, br, _bb = ink_box(ctx, base)
            for mark in marks:
                ml, _mt, mr, _mb = ink_box(ctx, mark)
                assert bl - 1 <= ml and mr <= br + 1, (size, block.text[base.cluster])


@pytest.mark.parametrize(("text", "language"), [(DEVANAGARI, "hi"), (ARABIC, "ar"), (THAI, "th"), (HEBREW, "he")])
def test_complex_scripts_keep_harfbuzz_positions(fonts: Any, text: str, language: str) -> None:
    """Only Latin/Greek/Cyrillic runs are grid-fitted: other scripts, and right-to-left runs, are unchanged."""
    for size in ctx_of(fonts).text_sizes():
        fitted = layout_text(text, fonts, width=300, size_px=size, language=language)
        plain = layout_text(text, fonts, width=300, size_px=size, language=language, grid_fit=False)
        assert fitted.glyphs == plain.glyphs and fitted.lines == plain.lines
        # Tracking and bold apply to neither: no grid-fitted run, no bold sibling.
        assert layout_text(text, fonts, width=300, size_px=size, language=language, tracking=3,
                           weight="bold").glyphs == fitted.glyphs


# --------------------------------------------------------------------------- line boxes


def test_tight_leading(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    ascii_text = "Hello World 2026, jumpy quiz (draft) [ok]; gjpqy Th0 " * 4
    stacked = "gjpqy ỮỞẪ Ặ gjpqy ỮỞẪ Ặ " * 4
    for size in ctx.text_sizes():
        font_a, font_d = ctx.line_box(size)
        strike = fonts.strike(ctx.base_face(size), size)
        assert (font_a, font_d) == (strike.ascent, strike.descent)
        ascent, descent = ctx.line_box(size, "tight")
        assert 0 < ascent <= font_a and 0 < descent <= font_d and ascent + descent < font_a + font_d
        for weight in weights(fonts, size):
            block = layout_text(ascii_text, fonts, width=220, size_px=size, leading="tight", weight=weight)
            assert len(block.lines) > 2
            for line in block.lines:
                assert (line.baseline - line.top, line.bottom - line.baseline) == (ascent, descent), (size, weight)
            assert [ln.top for ln in block.lines] == [k * (ascent + descent) for k in range(len(block.lines))]
            block = layout_text(stacked, fonts, width=220, size_px=size, leading="tight", language="vi",
                                weight=weight)
            assert any(ln.baseline - ln.top > ascent for ln in block.lines), "stacked capitals grow the line"
            for line, next_line in zip(block.lines, block.lines[1:], strict=False):
                assert next_line.top >= line.bottom
            for g in block.glyphs:
                line = block.lines[g.line]
                _left, top, _right, bottom = ink_box(ctx, g)
                assert line.top <= top and bottom <= line.bottom, (size, weight, block.text[g.cluster])
        assert layout_text("Hxg", fonts, width=100, size_px=size, leading=(3, 2)).lines[0].height >= 5
        explicit = layout_text("x", fonts, width=100, size_px=size, leading=(ascent + 5, descent + 1)).lines[0]
        assert (explicit.baseline - explicit.top, explicit.bottom - explicit.baseline) == (ascent + 5, descent + 1)


def test_sizes_and_line_box_arguments(fonts: Any, dev_fonts: Any) -> None:
    ctx = ctx_of(fonts)
    sizes = ctx.text_sizes()
    for want in (1, 11, 12, 13, 15, 16, 20, 24, 31, 32, 100):
        fitting = [s for s in sizes if s <= want]
        assert ctx.nearest_size(want) == (max(fitting) if fitting else min(sizes))
    dev = ctx_of(dev_fonts)
    assert dev.text_sizes() == (16, 24)
    assert [dev.nearest_size(w) for w in (12, 14, 16, 20, 24, 32)] == [16, 16, 16, 16, 24, 24]
    with pytest.raises(ValueError, match="nearest_size"):
        layout_text("x", dev_fonts, width=100, size_px=12)
    with pytest.raises(ValueError, match="leading"):
        layout_text("x", fonts, width=100, leading="loose")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="leading"):
        ctx.line_box(16, (-1, 4))
    with pytest.raises(ValueError, match="tracking"):
        layout_text("x", fonts, width=100, tracking=9)
    with pytest.raises(ValueError, match="weight"):
        layout_text("x", fonts, width=100, weight="heavy")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- tracking


def test_tracking_goes_between_clusters_only(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    for size in ctx.text_sizes():
        for weight in weights(fonts, size):
            base = layout_text("NEEDS YOU", fonts, width=1000, size_px=size, weight=weight)
            for k in (1, 2, 8):
                tracked = layout_text("NEEDS YOU", fonts, width=1000, size_px=size, weight=weight, tracking=k)
                # Nine clusters (the space counts), eight gaps, none after the last letter.
                assert tracked.width == base.width + 8 * k
                assert [g.x for g in tracked.glyphs] == [g.x + k * g.cluster for g in base.glyphs]
                assert measure_text("NEEDS YOU", fonts, size_px=size, weight=weight, tracking=k) == tracked.width
                # The measured width is exact: the label fits that box on one line, and the trailing
                # cluster of a wrapped line carries no tracking either.
                assert len(layout_text("NEEDS YOU", fonts, width=tracked.width, size_px=size, weight=weight,
                                       tracking=k).lines) == 1
                wrapped = layout_text("NEEDS YOU NOW", fonts, width=tracked.width, size_px=size, weight=weight,
                                      tracking=k)
                assert wrapped.text[wrapped.lines[0].start:wrapped.lines[0].end] == "NEEDS YOU"
                assert wrapped.lines[0].width == tracked.width
        # Tracked text never joins letters: no "fi" ligature.
        assert len(layout_text("fi", fonts, width=100, size_px=size).glyphs) == 1
        assert len(layout_text("fi", fonts, width=100, size_px=size, tracking=1).glyphs) == 2
        # Tracking applies to grid-fitted runs only.
        assert layout_text("NEEDS YOU", fonts, width=1000, size_px=size, tracking=2, grid_fit=False).glyphs \
            == layout_text("NEEDS YOU", fonts, width=1000, size_px=size, grid_fit=False).glyphs


def test_end_and_centre_aligned_text_keeps_its_ink_in_the_box(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    for size in ctx.text_sizes():
        for weight in weights(fonts, size):
            for text in ("2:05 PM", "Updated Sun 2:05 PM", "+4 MORE", "Sep 26"):
                for align in ("end", "center", "start"):
                    for tracking in (0, 1):
                        block = layout_text(text, fonts, width=150, size_px=size, weight=weight, align=align,
                                            tracking=tracking)
                        line = block.lines[0]
                        if align == "end":
                            assert line.x + line.width == 150
                        boxes = [ink_box(ctx, g) for g in block.glyphs]
                        assert min(b[0] for b in boxes) >= 0 and max(b[2] for b in boxes) <= 150, (text, align)


# --------------------------------------------------------------------------- bold


def test_bold_uses_the_bold_face_for_latin_greek_cyrillic_only(fonts: Any) -> None:
    ctx = ctx_of(fonts)
    sizes = [s for s in ctx.text_sizes() if ctx.has_weight("bold", s)]
    if not sizes:
        pytest.skip("this pack has no bold face")
    bold_face = next(f.face_id for f in fonts.faces if f.regular_face_id == 1 and f.weight == 700)
    assert ctx.regular_of(bold_face) == 1 and ctx.regular_of(1) == 1 and ctx.regular_of(5) == 5
    assert ctx.grid_fit(bold_face) and ctx.grid_fit(1) and not ctx.grid_fit(5) and not ctx.grid_fit(166)
    assert bold_face not in ctx.text_faces and bold_face in ctx.shapeable
    assert bold_face not in ctx.candidates("Latn", "", sizes[0])
    text = "Hello مرحبا 你好 Привет Γειά 123 😀"
    for size in sizes:
        assert ctx.has_weight("bold", size) and ctx.has_weight(700, size) and ctx.has_weight("regular", size)
        regular = layout_text(text, fonts, width=2000, size_px=size)
        bold = layout_text(text, fonts, width=2000, size_px=size, weight="bold")
        assert bold_face not in regular.faces
        face_of = {g.cluster: g.face_id for g in bold.glyphs}
        for i, ch in enumerate(bold.text):
            if i in face_of:
                if ch.isascii() or "Ͱ" <= ch <= "ӿ":
                    assert face_of[i] == bold_face, (size, ch)
                else:
                    assert face_of[i] != bold_face, (size, ch)  # Arabic, Han and emoji stay regular
        assert {f for f in bold.faces if f != bold_face} == {f for f in regular.faces if f != 1}
        assert bold.unsupported == regular.unsupported == () and bold.notdef == 0
        # The ellipsis of a bold line is bold too.
        cut = layout_text("Approve deployment of release 2.4 to production", fonts, width=120, size_px=size,
                          weight="bold", max_lines=1)
        assert cut.truncated and {g.face_id for g in cut.glyphs} == {bold_face}


def test_bold_is_regular_without_a_bold_face(dev_fonts: Any) -> None:
    ctx = ctx_of(dev_fonts)
    for size in ctx.text_sizes():
        assert not ctx.has_weight("bold", size) and ctx.has_weight("regular", size)
        for text in ("Approve deployment of release 2.4?", "Hello مرحبا שלום"):
            bold = layout_text(text, dev_fonts, width=300, size_px=size, weight="bold")
            assert bold == layout_text(text, dev_fonts, width=300, size_px=size)


# --------------------------------------------------------------------------- cache


def test_cache_keys_cover_the_new_parameters(fonts: Any) -> None:
    text = "Approve deployment of release 2.4 to production?"
    size = 24
    base = layout_text(text, fonts, width=200, size_px=size)
    assert layout_text(text, fonts, width=200, size_px=size) is base
    variants = {
        "tight": layout_text(text, fonts, width=200, size_px=size, leading="tight"),
        "explicit": layout_text(text, fonts, width=200, size_px=size, leading=(40, 10)),
        "tracking": layout_text(text, fonts, width=200, size_px=size, tracking=1),
        "26.6": layout_text(text, fonts, width=200, size_px=size, grid_fit=False),
    }
    if ctx_of(fonts).has_weight("bold", size):
        variants["bold"] = layout_text(text, fonts, width=200, size_px=size, weight="bold")
    for name, block in variants.items():
        assert block is not base and block != base, name
    assert len({id(b) for b in variants.values()}) == len(variants)
    engine_module._cache.clear()
    assert layout_text(text, fonts, width=200, size_px=size, tracking=1) == variants["tracking"]
