"""Panels and layout inspection shared by the composer tests (`test_screen`, `test_design`).

Everything here reads a composed layout back the way a tag would draw it: ink pixels come from the pack's
glyph bitmaps along the GLYPHS pen walk (§4.4), icons from face 0, QR modules from the symbol, and colours
from the reference renderer (`compose.preview.render_image`). Expected values are derived from whichever pack
is loaded (no pixel goldens: packs built on different systems differ by a few pixels).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

from app.tags.runtime.compose.api import ActiveCard, ComposedScreen, TagPanel
from app.tags.runtime.compose.preview import render_image
from app.tags.runtime.compose.screen import MAX_BYTES
from app.tags.runtime.layout import FontContext, command_glyphs
from app.tags.runtime.protocol.ids import (
    LAYOUT_HARD_MAX,
    LAYOUT_MAX_COMMANDS,
    LAYOUT_MAX_GLYPHS,
    LAYOUT_MAX_LINE_STEPS,
    LAYOUT_MAX_QR,
    LAYOUT_SERIAL_MAX,
    Color,
)
from app.tags.runtime.protocol.layout import (
    Glyphs,
    Icon,
    Layout,
    Line,
    Progress,
    Qr,
    Rect,
    check_panel,
    check_strikes,
    decode_layout,
    line_steps,
    qr_code,
)

LANDSCAPE_BW = TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 0, "Desk")
LANDSCAPE_BWR = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 0, "Desk")
PORTRAIT_BWR = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 1, "Kitchen")
HEMA = TagPanel(0xD1F06B9A, 128, 250, 2, 3, 3, "Hema")
"""The user's 2.13" black/white/red tag: native 128x250, rotation 3 -> logical 250x128 (XS landscape)."""
HEMA_BW = TagPanel(0xD1F06B9A, 128, 250, 1, 1, 3, "Hema")
HEMA_PORTRAIT = TagPanel(0xD1F06B9A, 128, 250, 2, 3, 0, "Hema")
SMALL_S = TagPanel(0x2C3D4E5F, 264, 176, 2, 3, 0, "Door")
"""S class (2.7")."""
SHELF = TagPanel(0x0BADCAFE, 296, 128, 1, 1, 0, "Small")
LARGE = TagPanel(0x1A2B3C4D, 800, 480, 2, 3, 0, "Wall")

ORDER = (501, 504, 503, 507, 505, 506, 502)
"""`samples.example_cards` in display order: needs_input 90, health 75, then priority 50/40/35/30 newest first."""
DENSITY = {"400x300": (7, 7), "300x400": (7, 7), "250x128": (3, 3), "128x250": (5, 4), "296x128": (3, 3),
           "264x176": (4, 3), "800x480": (7, 7)}
"""Least cards shown from the example set, by logical size: (pack with 12 px text, pack without). The design's
promise of more at a glance (the old composer showed 2 on the Hema, 4 on 400x300)."""

LONG = {
    "latin": "Approve the deployment of release candidate twelve to the production cluster tonight? ",
    "vietnamese": "Người dùng đã yêu cầu phê duyệt việc triển khai phiên bản mới lên máy chủ sản xuất. ",
    "arabic": "هل توافق على نشر الإصدار الجديد على خوادم الإنتاج الليلة؟ ",
    "hebrew": "האם לאשר את הפריסה של הגרסה החדשה לשרתי הייצור הלילה? ",
    "thai": "คุณต้องการอนุมัติการติดตั้งเวอร์ชันใหม่บนเซิร์ฟเวอร์คืนนี้หรือไม่ ",
    "devanagari": "क्या आप आज रात उत्पादन सर्वर पर नए संस्करण की तैनाती को स्वीकृति देते हैं? ",
    "tamil": "இன்றிரவு புதிய பதிப்பை உற்பத்தி சேவையகங்களில் நிறுவ ஒப்புதல் தருகிறீர்களா? ",
    "chinese": "您是否批准今晚将新版本部署到生产服务器？请尽快回复。",
    "japanese": "今夜、新しいバージョンを本番サーバーにデプロイすることを承認しますか？",
    "korean": "오늘 밤 새 버전을 프로덕션 서버에 배포하는 것을 승인하시겠습니까? ",
    "emoji": "🎉👍🏽❤️😀🚀🔥✅📦🧪🛠️ ",
    "narrow": "il1.,:;|!ij'" * 3,
    "alternating": "aب1אกक" * 4,
}
"""One sentence per script (and two stress texts), repeated into worst-case titles and bodies."""


def density(panel: TagPanel, fonts: Any) -> int:
    """`DENSITY` for ``panel`` and the pack ``fonts``."""
    w, h = (panel.width, panel.height) if panel.rotation % 2 == 0 else (panel.height, panel.width)
    new, old = DENSITY[f"{w}x{h}"]
    return new if 12 in FontContext.for_fontset(fonts).text_sizes() else old


def card_sets(now: datetime) -> dict[str, list[ActiveCard]]:
    """The gallery's card sets (dist/tag-design/render_gallery.py): every tone, kind and script a screen meets."""
    from app.tags.runtime.compose.samples import example_cards

    example = example_cards(now)
    dense = example + [card(801 + i, "notification", t, 40, 2 + 7 * i, now) for i, t in enumerate(
        ["Build #482 passed", "New comment on Q4 roadmap", "Invoice INV-2291 paid", "Backup verified: 14 GB",
         "Lan shared 3 files with you", "Deploy window opens at 16:00", "Disk usage at 81% on lab-02"])]
    return {
        "example": example,
        "test-card": [card(601, "pinned_note", "Hello from Cremind", 55, 0, now, icon="check_circle",
                           body="This tag is set up and receiving updates.")],
        "dense": dense,
        "error": [
            card(701, "automation", "Morning briefing automation failed", 70, 4, now, severity="error",
                 icon="error", body="The model provider returned an error: rate limit reached."),
            card(702, "indexing_problem", "Document indexing: paused", 45, 25, now, severity="warning",
                 icon="folder", body="The Documents folder is not reachable."),
            card(703, "task_outcome", "Reply ready: Budget review", 50, 12, now, severity="success", icon="chat"),
            card(704, "calendar", "Next: 14:00 Product sync", 35, 1, now, icon="event"),
            card(705, "tag_diagnostics", "Battery low", 20, 60, now, severity="warning", icon="battery_low",
                 body="2410 mV"),
        ],
        "vietnamese": [
            card(901, "needs_input", "Duyệt triển khai bản 2.4 lên môi trường production?", 90, 3, now,
                 severity="attention", icon="approval", lang="vi", body="Trò chuyện: Kế hoạch phát hành"),
            card(902, "health", "Kênh Zalo bị ngắt kết nối", 75, 40, now, severity="warning", icon="link_off",
                 lang="vi"),
            card(903, "task_outcome", "Đã có trả lời: Tóm tắt cuộc họp sáng nay", 50, 20, now,
                 severity="success", icon="chat", lang="vi"),
            card(904, "notification", "Lan đã chia sẻ 3 tệp với bạn", 40, 55, now, lang="vi"),
        ],
        "calm": [
            card(951, "notification", "Lan shared 3 files with you", 40, 8, now),
            card(952, "task_outcome", "Reply ready: Budget review", 50, 15, now, severity="success", icon="chat"),
            card(953, "calendar", "Next: 14:00 Product sync", 35, 2, now, icon="event"),
        ],
        "empty": [],
    }


def bw_twin(panel: TagPanel) -> TagPanel:
    """The same panel with one (black) plane."""
    return dataclasses.replace(panel, planes=1, plane_flags=1)


def card(did: int, kind: str, title: str, prio: int, minutes: int, now: datetime, **extra: Any) -> ActiveCard:
    ts = now - timedelta(minutes=minutes)
    return ActiveCard(did, kind, prio, ts, {"v": 1, "kind": kind, "title": title, "lang": extra.pop("lang", "en"),
                                            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra})


def render_cost(layout: Layout) -> tuple[int, int]:
    """QR commands and LINE steps of a layout (§4.3 render-cost bounds)."""
    return (sum(isinstance(c, Qr) for c in layout.commands),
            sum(line_steps(c) for c in layout.commands if isinstance(c, Line)))


def check(screen: ComposedScreen, panel: TagPanel, fonts: Any) -> Layout:
    """The invariants every composed screen keeps (limits, strikes, panel, render cost, delivery ids)."""
    assert len(screen.layout) <= MAX_BYTES == min(LAYOUT_HARD_MAX, LAYOUT_SERIAL_MAX) == 4000
    layout = decode_layout(screen.layout)
    check_strikes(layout, fonts.has_strike)
    check_panel(layout, panel.width, panel.height)
    assert len(layout.commands) <= LAYOUT_MAX_COMMANDS
    assert sum(len(c.glyphs) for c in layout.commands if isinstance(c, Glyphs)) <= LAYOUT_MAX_GLYPHS
    for cmd in layout.commands:
        if isinstance(cmd, Glyphs):
            for _gid, x, y in command_glyphs(cmd):
                assert -64 <= x < layout.width + 64 and -64 <= y < layout.height + 64
        if isinstance(cmd, Line):  # inside the canvas, well within the §4.3 endpoint box
            assert 0 <= min(cmd.x0, cmd.x1) and max(cmd.x0, cmd.x1) < layout.width
            assert 0 <= min(cmd.y0, cmd.y1) and max(cmd.y0, cmd.y1) < layout.height
    qrs, steps = render_cost(layout)
    assert qrs <= 1 <= LAYOUT_MAX_QR and steps <= 3 * layout.width <= LAYOUT_MAX_LINE_STEPS
    assert screen.pending_count == len(screen.pending_delivery_ids)
    assert not set(screen.delivery_ids) & set(screen.pending_delivery_ids)
    return layout


# --------------------------------------------------------------------------- pixels


def _bitmap_pixels(glyph: Any, left: int, top: int) -> Iterator[tuple[int, int]]:
    row_bytes = (glyph.width + 7) // 8
    for row in range(glyph.height):
        bits = glyph.bitmap[row * row_bytes:(row + 1) * row_bytes]
        for col in range(glyph.width):
            if bits[col >> 3] & (0x80 >> (col & 7)):
                yield left + col, top + row


def ink(cmd: Any, fonts: Any) -> set[tuple[int, int]]:
    """Logical pixels a command paints (a RECT's whole area, a PROGRESS bar's box)."""
    pack = FontContext.for_fontset(fonts).pack
    out: set[tuple[int, int]] = set()
    if isinstance(cmd, Glyphs):
        for gid, x, y in command_glyphs(cmd):
            g = pack.glyph(cmd.face, cmd.size_px, gid)
            if g is not None and g.bitmap:
                out.update(_bitmap_pixels(g, x + g.bearing_x, y - g.bearing_y))
    elif isinstance(cmd, Icon):
        g = pack.glyph(0, cmd.size_px, cmd.icon)
        if g is not None and g.bitmap:
            out.update(_bitmap_pixels(g, cmd.x, cmd.y))
    elif isinstance(cmd, Qr):
        symbol = qr_code(cmd.text, cmd.ecc)
        m = cmd.module_px
        for my in range(symbol.get_size()):
            for mx in range(symbol.get_size()):
                if symbol.get_module(mx, my):
                    out.update((cmd.x + mx * m + i, cmd.y + my * m + j) for i in range(m) for j in range(m))
    elif isinstance(cmd, Progress | Rect):
        out.update((cmd.x + i, cmd.y + j) for i in range(cmd.w) for j in range(cmd.h))
    return out


def colours(layout: Layout | bytes, fonts: Any, panel: TagPanel) -> tuple[int, int, bytes]:
    """(width, height, one colour per logical pixel: 0 white, 1 black, 2 red) as the reference renderer draws."""
    img = render_image(layout, fonts, panel=panel)
    data = img.tobytes() if img.mode == "P" else bytes(0 if v else 1 for v in img.convert("L").tobytes())
    return img.width, img.height, data


def background(layout: Layout) -> Layout:
    """The layout without its text and icons (what text is drawn over)."""
    return dataclasses.replace(layout, commands=tuple(c for c in layout.commands
                                                      if not isinstance(c, Glyphs | Icon)))


def with_red_as_black(layout: Layout) -> tuple[Any, ...]:
    return tuple(dataclasses.replace(c, color=Color.BLACK) if getattr(c, "color", None) == Color.RED else c
                 for c in layout.commands)
